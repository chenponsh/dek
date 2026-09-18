#!/usr/bin/python3
"""Offline verifier and atomic installer for the independently supplied initial release."""
import argparse, json, os, pathlib, shutil, stat, subprocess, sys, tempfile
_INSTALL_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INSTALL_ROOT) not in sys.path: sys.path.insert(0, str(_INSTALL_ROOT))
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from deploy.activator import Activator, ActivatorConfig, atomic_json, file_digest, fsync_tree
from deploy.release_bundle import _canonical_release

def _check_imports():
 for module in (sys.modules[Activator.__module__],sys.modules[_canonical_release.__module__]):
  if not pathlib.Path(module.__file__).resolve(strict=True).is_relative_to(_INSTALL_ROOT): raise SystemExit("security-critical import escaped installed root")

def active_metadata(meta):
 required=("nonce","generation","commit","tree","bundle_sha256","artifacts")
 if any(key not in meta for key in required): raise SystemExit("release metadata incomplete")
 value={"schema_version":2,"sequence":1,"nonce":meta["nonce"],"generation":meta["generation"],"previous_generation":None,
        "commit":meta["commit"],"tree":meta["tree"],"bundle_sha256":meta["bundle_sha256"],"artifacts":meta["artifacts"]}
 provenance=("decision_id","decision_sha256","origin")
 if all(key in meta for key in provenance): value.update({key:meta[key] for key in provenance})
 Activator._validate_metadata(value)
 return value

def _committed_sequences(value):
 """Yield every integer ``sequence`` committed anywhere in a state JSON tree."""
 if isinstance(value, dict):
  for key, item in value.items():
   if key == "sequence" and isinstance(item, int) and not isinstance(item, bool): yield item
   else: yield from _committed_sequences(item)
 elif isinstance(value, list):
  for item in value: yield from _committed_sequences(item)

def _read_committed_json(path):
 try:
  return json.loads(path.read_text(encoding="utf-8"))
 except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
  raise SystemExit(f"committed state unreadable: {path}") from exc

def _iter_state_sources(active, releases, journal, spent, outcomes):
 active=pathlib.Path(active)
 if active.exists(): yield active
 releases=pathlib.Path(releases)
 if releases.exists():
  for release_dir in sorted(releases.iterdir()):
   meta=release_dir/"release.json"
   if release_dir.is_dir() and meta.exists(): yield meta
 for directory in (journal, spent, outcomes):
  directory=pathlib.Path(directory)
  if directory.exists():
   for path in sorted(directory.glob("*.json")): yield path

def global_high_sequence(active, releases, journal, spent, outcomes):
 """Highest committed sequence across every state source; ``None`` when virgin."""
 highest=None
 for path in _iter_state_sources(active, releases, journal, spent, outcomes):
  for seq in _committed_sequences(_read_committed_json(path)):
   if highest is None or seq > highest: highest=seq
 return highest

def assert_seed_monotonic(active, releases, journal, spent, outcomes, descriptor):
 """Fail closed when reseeding would commit a sequence below an existing one."""
 high=global_high_sequence(active, releases, journal, spent, outcomes)
 if high is not None and high > descriptor["sequence"]:
  raise SystemExit(f"seed sequence {descriptor['sequence']} would downgrade committed sequence {high}")

def active_is_exact(active, descriptor):
 active=pathlib.Path(active)
 if not active.exists(): return False
 expected=(json.dumps(descriptor,sort_keys=True,separators=(",",":"))+"\n").encode()
 try:
  raw=active.read_bytes()
  if raw!=expected or json.loads(raw)!=descriptor: raise ValueError
 except (OSError,ValueError,json.JSONDecodeError,UnicodeDecodeError): raise SystemExit("active descriptor mismatch")
 return True

def install_active(active, descriptor):
 active=pathlib.Path(active); active.parent.mkdir(parents=True,exist_ok=True)
 expected=(json.dumps(descriptor,sort_keys=True,separators=(",",":"))+"\n").encode()
 if active_is_exact(active,descriptor): return
 try:
  fd=os.open(active,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
 except FileExistsError:
  try:
   raw=active.read_bytes()
   if raw!=expected or json.loads(raw)!=descriptor: raise ValueError
  except (OSError,ValueError,json.JSONDecodeError,UnicodeDecodeError): raise SystemExit("active descriptor mismatch")
  return
 try:
  with os.fdopen(fd,"wb") as handle: handle.write(expected); handle.flush(); os.fsync(handle.fileno())
  descriptor_fd=os.open(active.parent,os.O_RDONLY|os.O_DIRECTORY)
  try: os.fsync(descriptor_fd)
  finally: os.close(descriptor_fd)
 except Exception:
  active.unlink(missing_ok=True)
  raise

def install_generation(src, target, descriptor, *, verify_release=None):
 src,target=pathlib.Path(src),pathlib.Path(target)
 if verify_release is not None: verify_release(src)
 def verify_existing():
  try:
   if json.loads((target/"release.json").read_text()) != descriptor: raise ValueError
   for relative,digest in descriptor["artifacts"].items():
    if file_digest(target/relative)!=digest: raise ValueError
   source_meta=json.loads((src/"release.json").read_text())
   installed_meta=json.loads((target/"build-release.json").read_text())
   if source_meta!=installed_meta: raise ValueError
  except (OSError,ValueError,json.JSONDecodeError): raise SystemExit("existing generation content mismatch")
 if target.exists(): verify_existing(); return
 staging=pathlib.Path(tempfile.mkdtemp(prefix=".seed-",dir=target.parent)); shutil.rmtree(staging)
 try:
  shutil.copytree(src,staging,copy_function=shutil.copy2)
  if verify_release is not None and verify_release(staging) != json.loads((src/"release.json").read_text()):
   raise SystemExit("copied release metadata changed")
  os.replace(staging/"release.json",staging/"build-release.json")
  atomic_json(staging/"release.json",descriptor,0o440)
  (staging/"release.lock").touch(exist_ok=True)
  for x in sorted(staging.rglob("*"),reverse=True): os.chmod(x,0o550 if x.is_dir() else 0o440)
  os.chmod(staging,0o550); fsync_tree(staging); os.replace(staging,target)
  descriptor_fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
  try: os.fsync(descriptor_fd)
  finally: os.close(descriptor_fd)
 except Exception:
  if staging.exists(): shutil.rmtree(staging)
  raise

def main(argv=None):
 _check_imports()
 p=argparse.ArgumentParser(); p.add_argument("--release",type=pathlib.Path,required=True); p.add_argument("--public-key",type=pathlib.Path,required=True); p.add_argument("--releases",type=pathlib.Path,required=True); p.add_argument("--active",type=pathlib.Path,required=True); p.add_argument("--expected-commit",required=True); p.add_argument("--expected-tree",required=True); p.add_argument("--expected-bundle-sha256",required=True); a=p.parse_args(argv)
 src=a.release.resolve(strict=True); meta=json.loads((src/"release.json").read_text()); key=load_pem_public_key(a.public_key.read_bytes())
 if not isinstance(key,Ed25519PublicKey): raise SystemExit("approval key is not Ed25519")
 key.verify((src/"release.sig").read_bytes(),_canonical_release(meta))
 for k,v in (("commit",a.expected_commit),("tree",a.expected_tree),("bundle_sha256",a.expected_bundle_sha256)):
  if meta.get(k)!=v: raise SystemExit(k+" mismatch")
 bundle=src/"repository.bundle"
 if file_digest(bundle)!=a.expected_bundle_sha256: raise SystemExit("bundle digest mismatch")
 subprocess.run(["git","bundle","verify",str(bundle)],check=True,env={"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"})
 with tempfile.TemporaryDirectory() as td:
  subprocess.run(["git","clone","--quiet",str(bundle),td],check=True,env={"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"})
  got_commit=subprocess.check_output(["git","-C",td,"rev-parse",a.expected_commit+"^{commit}"],text=True).strip()
  got_tree=subprocess.check_output(["git","-C",td,"rev-parse",a.expected_commit+"^{tree}"],text=True).strip()
 if got_commit!=a.expected_commit or got_tree!=a.expected_tree: raise SystemExit("bundle object mismatch")
 cfg=ActivatorConfig.under(a.releases.parent); validator=Activator(cfg,proof_reader=lambda *_:{},approval_key=key); validator._validate_release(src,confined=False)
 descriptor=active_metadata(meta)
 assert_seed_monotonic(a.active, a.releases, cfg.journal, cfg.spent, cfg.outcomes, descriptor)  # fail closed against every state source
 target=a.releases/meta["generation"]
 active_is_exact(a.active,descriptor)  # fail closed before any install mutation
 install_generation(src,target,descriptor,verify_release=lambda path: validator._validate_release(path,confined=False))
 install_active(a.active,descriptor)
 return 0
if __name__=="__main__": raise SystemExit(main())
