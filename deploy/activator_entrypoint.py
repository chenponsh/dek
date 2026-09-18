#!/usr/bin/python3
"""Fixed one-shot runner for the non-root activator."""
import argparse
import json
import os
import time
import urllib.request
import sys
import hashlib
import stat
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
from deploy.activator import ActivationError, FatalActivationError, Activator, ActivatorConfig, atomic_json
from web.review import queue_lock
from cryptography.hazmat.primitives.serialization import load_pem_public_key


def _scan_candidate_dirs(build_inbox: Path) -> list[Path]:
    """Real activation candidates under build_inbox, skipping dot-prefixed
    entries (BundleBuilder's own builds/.builder-failures quarantine is not
    readable by this identity and is not a candidate) and incomplete dirs."""
    return sorted(
        path for path in build_inbox.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
        and (path / "activation-ready.json").is_file() and (path / "activation-ready.sig").is_file()
    )


def activate_candidates(activator, candidates, *, on_failure=None):
    attempted=0; succeeded=0
    items=list(candidates)
    if items and all(len(item)==4 for item in items):
        items=sorted(items)
    for item in items:
        candidate=item[-1]
        attempted+=1
        try:
            activator.activate(candidate); succeeded+=1
        except FatalActivationError:
            raise
        except Exception as exc:
            if on_failure is not None:
                on_failure(candidate,exc)
            print(f"DEK activation failed candidate={candidate.name} error_type={type(exc).__name__}",file=sys.stderr)
    if attempted and not succeeded:
        raise ActivationError("all pending activations failed")


def order_candidates_by_ancestry(candidates, active):
    """Return the unique commit chain rooted at active; IDs never affect order.

    A generation with no "decision_id" (the from-source seed, or a rollback
    target predating the review pipeline) was never itself a reviewed
    decision, so no real decision's parent_commit can ever be expected to
    equal its commit -- every commit since bootstrap, reviewed or not, moved
    the repo forward without that generation's involvement. Treat it the
    same as "no active generation yet": any single unambiguous candidate may
    bootstrap past it. Once a real, decision-derived generation is active,
    the strict commit-to-commit chain applies as normal.
    """
    remaining=list(candidates); ordered=[]
    current=active.get("commit") if isinstance(active,dict) and "decision_id" in active else None
    if current is None:
        if len(remaining) > 1:
            raise FatalActivationError("ambiguous bootstrap publication order")
        return remaining
    while remaining:
        matches=[item for item in remaining if item[0].get("parent_commit")==current]
        if not matches:
            break
        if len(matches)!=1:
            raise FatalActivationError("ambiguous publication ancestry")
        selected=matches[0]; ordered.append(selected); remaining.remove(selected)
        current=selected[0].get("commit")
    return ordered


def verify_gate_queue_snapshot(gate: dict, queue: Path) -> None:
    descriptor=None
    try:
        descriptor=os.open(queue,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        details=os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink!=1 or details.st_size>1024*1024*1024:
            raise ActivationError("decision queue snapshot source is unsafe")
        digest=hashlib.sha256(); total=0
        while True:
            chunk=os.read(descriptor,65536)
            if not chunk: break
            digest.update(chunk); total+=len(chunk)
    except OSError as exc:
        raise ActivationError("decision queue snapshot source is unreadable") from exc
    finally:
        if descriptor is not None: os.close(descriptor)
    if (gate.get("decision_queue_sha256")!=digest.hexdigest()
            or gate.get("decision_queue_size")!=total):
        raise ActivationError("decision queue snapshot no longer current")


def isolate_activation_candidate(config: ActivatorConfig, candidate: Path, exc: BaseException) -> None:
    digest=hashlib.sha256(candidate.name.encode("utf-8","surrogateescape")).hexdigest()
    atomic_json(config.control/"candidate-failures"/(digest+".json"),
                {"schema_version":1,"status":"isolated","candidate":candidate.name,
                 "error_type":type(exc).__name__},0o600)


class QueueBoundActivator:
    def __init__(self, activator, queue: Path):
        self.activator, self.queue = activator, Path(queue)

    def activate(self, candidate: Path):
        # The append writer takes this same exclusive lock.  Keep it through the
        # active-pointer mutation so a later return/reject cannot race the check.
        with queue_lock(self.queue,read_only=True):
            gate=json.loads((candidate/"activation-ready.json").read_text(encoding="utf-8"))
            verify_gate_queue_snapshot(gate,self.queue)
            return self.activator.activate(candidate)


def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,required=True); args=parser.parse_args(argv)
    value=json.loads(args.config.read_text(encoding="utf-8"))
    required={"build_inbox","releases","control","journal","outcomes","spent","active","qa_proof","web_proof_url","web_proof_secret_file","approval_public_key","decision_queue"}
    if set(value)!=required: raise SystemExit("invalid activator config")
    config=ActivatorConfig(*(Path(value[key]) for key in ("build_inbox","releases","control","journal","outcomes","spent","active")))
    def proof(kind, expected):
        if kind == "qa":
            deadline=time.monotonic()+5
            while True:
                try:
                    found=json.loads(Path(value["qa_proof"]).read_text(encoding="utf-8"))
                    if found.get("nonce")==expected.get("nonce"): return found
                except (OSError,json.JSONDecodeError): pass
                if time.monotonic()>=deadline: return {}
                time.sleep(.1)
        secret_path=value["web_proof_secret_file"].replace("%d",os.environ.get("CREDENTIALS_DIRECTORY",""))
        secret=Path(secret_path).read_text(encoding="utf-8").strip()
        request=urllib.request.Request(value["web_proof_url"],headers={"X-DEK-Generation-Proof":secret})
        with urllib.request.urlopen(request,timeout=5) as response: return json.loads(response.read())
    approval_key=load_pem_public_key(Path(value["approval_public_key"]).read_bytes())
    activator=Activator(config,proof_reader=proof,approval_key=approval_key); activator.reconcile()
    candidates=[]
    for path in _scan_candidate_dirs(config.build_inbox):
        try:
            metadata=activator._validate_release(path); nonce=metadata.get("nonce")
            if isinstance(nonce,str) and not (config.spent/(nonce+".json")).exists():
                candidates.append((metadata,path))
        except FatalActivationError:
            raise
        except Exception as exc:
            isolate_activation_candidate(config,path,exc)
            continue
    ordered=order_candidates_by_ancestry(candidates,activator._read_active(required=False))
    selected={path for _,path in ordered}
    for _,path in candidates:
        if path not in selected:
            isolate_activation_candidate(config,path,ActivationError("publication ancestry mismatch"))
    activate_candidates(QueueBoundActivator(activator,Path(value["decision_queue"])),ordered,
                        on_failure=lambda path,exc:isolate_activation_candidate(config,path,exc))
    return 0

if __name__ == "__main__": raise SystemExit(main())
