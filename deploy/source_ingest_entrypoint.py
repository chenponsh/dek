#!/usr/bin/python3
"""Run ingestion in a new fixed-origin clone and export only review input."""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import base64
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path

_INSTALL_ROOT = Path(__file__).resolve().parent.parent
if str(_INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(_INSTALL_ROOT))
# `python3 -I` (this script's production invocation) ignores PYTHONPATH by
# design -- it still reaches os.environ, so systemd's Environment=PYTHONPATH=
# setting for the vendored Playwright install has to be applied explicitly,
# the same way _INSTALL_ROOT is above, rather than relying on the
# interpreter's normal (here suppressed) auto-import of it.
for _vendor_entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    if _vendor_entry and _vendor_entry not in sys.path:
        sys.path.insert(0, _vendor_entry)
from deploy.release_bundle import validate_systemd_credential, PROXY_ENV_KEYS
from deploy.fsutil import atomic_write_bytes, atomic_write_json, read_bounded_regular
if not Path(sys.modules[validate_systemd_credential.__module__].__file__).resolve(strict=True).is_relative_to(_INSTALL_ROOT):
    raise SystemExit("security-critical import escaped installed root")


REPORT_PATTERN = "scheduled-run-*_*.json"
PROOF_ROOT = Path("/var/lib/dek-source-ingest/proofs")
PROOF_NAMES = frozenset({"latest-report.json", "staged-proof-report.json"})
EXPECTED_NAMES = frozenset({"latest-report.expected.json", "staged-proof-report.expected.json"})
MAX_RUN_DURATION = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)


def load_ingestion_modules(package_root: Path):
    """Load ingestion from one explicit package without consulting live sys.path."""
    package_root = Path(package_root).resolve(strict=True)
    ingestion = package_root / "ingestion"
    automation = ingestion / "automation"
    required = [ingestion / "__init__.py", automation / "__init__.py"] + [
        automation / f"{name}.py" for name in ("cli", "core", "fetchers", "audit", "sources")
    ]
    if any(not path.is_file() or path.is_symlink() for path in required):
        raise SystemExit("candidate ingestion package is incomplete or unsafe")
    namespace = "_dek_ingestion_" + hashlib.sha256(str(package_root).encode()).hexdigest()[:16]
    for name, init, locations in (
        (namespace, ingestion / "__init__.py", [str(ingestion)]),
        (namespace + ".automation", automation / "__init__.py", [str(automation)]),
    ):
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=locations)
        if spec is None or spec.loader is None:
            raise SystemExit("candidate ingestion package cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    modules = tuple(importlib.import_module(namespace + ".automation." + name)
                    for name in ("cli", "core", "fetchers", "audit"))
    # cli imports sources too; it is not returned, but must come from the package as well.
    modules_checked = modules + (importlib.import_module(namespace + ".automation.sources"),)
    if any(not Path(module.__file__).resolve(strict=True).is_relative_to(package_root) for module in modules_checked):
        raise SystemExit("security-critical import escaped candidate package")
    return modules


def publication_plan(*, pre_cutover: bool, changed: bool) -> tuple[str, ...]:
    """Make the pre-cutover remote-mutation prohibition explicit and testable."""
    if pre_cutover or not changed:
        return ()
    return ("commit", "push", "bundle")


def _report_bytes(path: Path, maximum: int = 16 * 1024 * 1024) -> bytes:
    try:
        return read_bounded_regular(path, maximum=maximum, require_nlink1=False)
    except OSError as exc:
        raise RuntimeError("ingestion report is not a bounded regular file") from exc


def report_snapshot(repo: Path) -> dict[Path, str]:
    report_root = repo / "_" / "ingestion"
    if not report_root.exists():
        return {}
    return {
        path: hashlib.sha256(_report_bytes(path)).hexdigest()
        for path in sorted(report_root.glob(REPORT_PATTERN))
    }


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError("current ingestion report generated_at is not strict UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError("current ingestion report generated_at is invalid") from exc
    if _utc_text(parsed) != value:
        raise RuntimeError("current ingestion report generated_at is not canonical UTC")
    return parsed


def _atomic_private_json(path: Path, value: dict) -> None:
    atomic_write_json(path, value, mode=0o600, prefix=".invocation-", ensure_parent_mode=0o700)


def persist_current_report(repo: Path, before: dict[Path, str], output: Path, *,
                           run_nonce: str, started_at: datetime, now: datetime | None = None) -> Path:
    if (not isinstance(run_nonce, str) or
            re.fullmatch(r"[A-Za-z0-9_-]{32,128}", run_nonce) is None):
        raise RuntimeError("invalid ingestion invocation nonce")
    if not isinstance(started_at, datetime) or started_at.tzinfo is None:
        raise RuntimeError("invalid ingestion invocation start time")
    started_at = started_at.astimezone(timezone.utc)
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    after = report_snapshot(repo)
    changed = [path for path, digest in after.items() if before.get(path) != digest]
    if len(changed) != 1:
        raise RuntimeError("ingestion run must produce exactly one current report")
    selected = changed[0]
    raw = _report_bytes(selected)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("current ingestion report is invalid JSON") from exc
    if (not isinstance(value, dict) or value.get("mode") != "scheduled-run"
            or not isinstance(value.get("date"), str) or not value["date"]
            or not isinstance(value.get("generated_at"), str) or not value["generated_at"]
            or value.get("run_nonce") != run_nonce
            or not isinstance(value.get("report"), dict)
            or not isinstance(value.get("rough_created"), list)):
        raise RuntimeError("current ingestion report has an invalid schema")
    generated_at = _parse_utc(value["generated_at"])
    if (generated_at < started_at or generated_at > checked_at + MAX_CLOCK_SKEW
            or generated_at - started_at > MAX_RUN_DURATION):
        raise RuntimeError("current ingestion report is outside this invocation window")
    atomic_write_bytes(output, raw, mode=0o600, prefix=".report-", ensure_parent_mode=0o700)
    return selected


REVIEW_INPUT = Path("/var/lib/dek-review/input")
INGEST_SUMMARY_NAME = "source-ingest-summary.json"
ACTIVE_CONTROL = Path("/run/dek-active-control/active.json")
ACTIVE_RELEASES = Path("/run/dek-active-releases")
MAX_ACTIVE_METADATA_BYTES = 1024 * 1024
MAX_REPOSITORY_BUNDLE_BYTES = 1024 * 1024 * 1024


def write_ingest_summary(report_path: Path, review_input: Path = REVIEW_INPUT, *,
                         started_at: datetime | None = None,
                         completed_at: datetime | None = None) -> Path:
    """Publish only reviewer-facing counts from an already validated report."""
    try:
        report = json.loads(_report_bytes(report_path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("validated ingestion report can no longer be read") from exc
    rough = report.get("rough_created") if isinstance(report, dict) else None
    updated = report.get("updated_count", 0) if isinstance(report, dict) else None
    generated = report.get("generated_at") if isinstance(report, dict) else None
    if (not isinstance(generated, str)
            or not isinstance(rough, list) or len(rough) > 10_000
            or any(not isinstance(item, str) for item in rough)
            or not isinstance(updated, int) or isinstance(updated, bool)
            or updated < 0 or updated > 10_000):
        raise RuntimeError("ingestion report has invalid change counts")
    payload = {
        "schema": 1,
        "generated_at": generated,
        "new_count": len(set(rough)),
        "updated_count": updated,
    }
    if started_at is not None or completed_at is not None:
        if (started_at is None or completed_at is None
                or started_at.tzinfo is None or completed_at.tzinfo is None):
            raise RuntimeError("ingestion summary times must be timezone-aware")
        started_at = started_at.astimezone(timezone.utc)
        completed_at = completed_at.astimezone(timezone.utc)
        if completed_at < started_at or completed_at - started_at > MAX_RUN_DURATION:
            raise RuntimeError("ingestion summary duration is invalid")
        payload.update({"schema": 2, "started_at": _utc_text(started_at),
                        "completed_at": _utc_text(completed_at)})
    output = review_input / INGEST_SUMMARY_NAME
    atomic_write_json(output, payload, mode=0o640, prefix=".ingest-summary-")
    return output


def refresh_review_bundle_from_active(active_path: Path = ACTIVE_CONTROL,
                                      releases_root: Path = ACTIVE_RELEASES,
                                      review_input: Path = REVIEW_INPUT) -> None:
    """Atomically reuse the immutable bundle from the proved active release."""
    try:
        active_raw=read_bounded_regular(active_path,maximum=MAX_ACTIVE_METADATA_BYTES)
        active=json.loads(active_raw)
    except (OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise SystemExit("active release metadata is unavailable") from exc
    generation=str(active.get("generation","")) if isinstance(active,dict) else ""
    commit=str(active.get("commit","")) if isinstance(active,dict) else ""
    tree=str(active.get("tree","")) if isinstance(active,dict) else ""
    digest=str(active.get("bundle_sha256","")) if isinstance(active,dict) else ""
    if (not re.fullmatch(r"[A-Za-z0-9_-]{17,159}",generation)
            or not re.fullmatch(r"[0-9a-f]{40,64}",commit)
            or not re.fullmatch(r"[0-9a-f]{40,64}",tree)
            or not re.fullmatch(r"[0-9a-f]{64}",digest)):
        raise SystemExit("active release metadata is invalid")
    releases=Path(releases_root).resolve(strict=True)
    candidate=releases/generation
    if candidate.is_symlink():
        raise SystemExit("active release path is unsafe")
    release=candidate.resolve(strict=True)
    if release.parent!=releases:
        raise SystemExit("active release path is unsafe")
    try:
        release_meta=json.loads(read_bounded_regular(release/"release.json",maximum=MAX_ACTIVE_METADATA_BYTES))
    except (OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise SystemExit("active release is unreadable") from exc
    if any(release_meta.get(name)!=active.get(name) for name in ("generation","commit","tree","bundle_sha256")):
        raise SystemExit("active release identity mismatch")
    source=release/"repository.bundle"
    try:
        details=source.lstat()
    except OSError as exc:
        raise SystemExit("active repository bundle is unreadable") from exc
    if (not stat.S_ISREG(details.st_mode) or details.st_nlink!=1
            or details.st_size<=0 or details.st_size>MAX_REPOSITORY_BUNDLE_BYTES):
        raise SystemExit("active repository bundle is unsafe")
    listed=subprocess.run(
        ("/usr/bin/git","--no-pager","-c","core.hooksPath=/dev/null","-c","credential.helper=",
         "-c","protocol.allow=never","-c","protocol.file.allow=always","bundle","list-heads",str(source),"refs/heads/main"),
        env={"HOME":"/var/empty/dek-source-ingest","PATH":"/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8",
             "GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_GLOBAL":"/dev/null",
             "GIT_TERMINAL_PROMPT":"0","GIT_ASKPASS":"/bin/false"},
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=False)
    if listed.returncode or listed.stdout.decode("utf-8","replace").strip()!=f"{commit} refs/heads/main":
        raise SystemExit("active repository bundle has no matching main branch")
    review_input=Path(review_input); staging=None; actual=hashlib.sha256()
    try:
        descriptor,name=tempfile.mkstemp(prefix=".repository-active-",suffix=".bundle",dir=review_input)
        staging=Path(name)
        with os.fdopen(descriptor,"wb") as target, source.open("rb") as incoming:
            for chunk in iter(lambda:incoming.read(1024*1024),b""):
                actual.update(chunk); target.write(chunk)
            target.flush(); os.fsync(target.fileno())
        if actual.hexdigest()!=digest:
            raise SystemExit("active repository bundle digest mismatch")
        if read_bounded_regular(active_path,maximum=MAX_ACTIVE_METADATA_BYTES)!=active_raw:
            raise SystemExit("active release changed during snapshot refresh")
        os.chmod(staging,0o640); os.replace(staging,review_input/"repository.bundle"); staging=None
        directory=os.open(review_input,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def write_review_bundle(run, git, repo: Path, temporary: str, review_input: Path | None = None) -> None:
    """The reviewers' snapshot: a bundle of this clone's `main`, swapped in atomically.

    The review pages derive everything from it (which drafts are pending, which approvals are
    already published), so it is rewritten after every ingest run and after every publish."""
    review_input = review_input or REVIEW_INPUT
    staging=review_input/(".repository-"+Path(temporary).name+".bundle")
    run(*git,"bundle","create",str(staging),"refs/heads/main")
    with staging.open("rb") as handle: os.fsync(handle.fileno())
    os.chmod(staging,0o640); os.replace(staging,review_input/"repository.bundle")
    shutil.rmtree(temporary)


def main(argv=None):
    invocation_started = datetime.now(timezone.utc)
    run_nonce = secrets.token_urlsafe(32)
    parser = argparse.ArgumentParser()
    parser.add_argument("--isolated-clone", type=Path)
    parser.add_argument("--origin")
    parser.add_argument("--proof-output", type=Path)
    parser.add_argument("--expected-output", type=Path)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--pre-cutover-proof", action="store_true")
    parser.add_argument("command", choices=("scheduled-run", "refresh-bundle", "refresh-active-bundle"))
    args = parser.parse_args(argv)
    installed = args.package_root.resolve(strict=True)
    if installed != _INSTALL_ROOT:
        raise SystemExit("package root does not match the executing entrypoint")
    if args.command=="refresh-active-bundle":
        refresh_review_bundle_from_active()
        return 0
    if any(value is None for value in (args.isolated_clone,args.origin,args.proof_output,args.expected_output)):
        raise SystemExit("clone and proof paths are required for ingestion")
    fixed=os.environ.get("DEK_FIXED_ORIGIN","")
    if args.origin != fixed or not fixed.startswith("https://"): raise SystemExit("origin is not fixed service policy")
    credential=Path(os.environ["DEK_GIT_CREDENTIAL_FILE"])
    validate_systemd_credential(credential)
    clone_root=args.isolated_clone.resolve(strict=True)
    try:
        proof_details = os.lstat(PROOF_ROOT)
    except OSError as exc:
        raise SystemExit("proof output root is unavailable") from exc
    if (not args.proof_output.is_absolute() or args.proof_output.name not in PROOF_NAMES
            or args.proof_output.parent != PROOF_ROOT
            or not stat.S_ISDIR(proof_details.st_mode) or stat.S_ISLNK(proof_details.st_mode)
            or proof_details.st_uid != os.geteuid() or stat.S_IMODE(proof_details.st_mode) != 0o700):
        raise SystemExit("proof output is not fixed service policy")
    if (not args.expected_output.is_absolute() or args.expected_output.name not in EXPECTED_NAMES
            or args.expected_output.parent != PROOF_ROOT
            or args.expected_output.name != args.proof_output.name.replace(".json", ".expected.json")):
        raise SystemExit("expected output is not fixed service policy")
    if args.command == "scheduled-run":
        _atomic_private_json(args.expected_output, {
            "schema": 1, "run_nonce": run_nonce, "started_at": _utc_text(invocation_started),
            "proof_output": str(args.proof_output),
        })
    temporary=tempfile.mkdtemp(prefix="run-",dir=clone_root); repo=Path(temporary)/"repo"
    # A real, writable-but-empty per-run HOME -- not the deliberately
    # unwritable /var/empty/dek-source-ingest this used to point git (and,
    # since it was applied process-wide via os.environ.update() below, also
    # every later subprocess including Chromium) at. Chromium's startup
    # needs a writable $HOME even with --disable-crash-reporter passed and
    # refused to launch at all with "chrome_crashpad_handler: --database is
    # required" -- confirmed by direct reproduction, reverting to reach the
    # real /var/empty/dek-source-ingest, which reliably reproduced it.
    home=Path(temporary)/"home"; home.mkdir(mode=0o700)
    parsed=urlsplit(credential.read_text(encoding="utf-8").strip()); target=urlsplit(fixed)
    if parsed.scheme!="https" or parsed.hostname!=target.hostname or parsed.path!=target.path or parsed.username is None or parsed.password is None: raise SystemExit("credential is not bound to fixed origin")
    auth="Authorization: Basic "+base64.b64encode(f"{parsed.username}:{parsed.password}".encode()).decode()
    git=("/usr/bin/git","--no-pager","-c","core.hooksPath=/dev/null","-c","credential.helper=","-c","credential.interactive=never","-c","core.fsmonitor=false","-c","core.sshCommand=","-c","diff.external=","-c","protocol.allow=never","-c","protocol.https.allow=always")
    environment={"HOME":str(home),"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_GLOBAL":"/dev/null","GIT_ATTR_NOSYSTEM":"1","GIT_TERMINAL_PROMPT":"0","GIT_ASKPASS":"/bin/false","SSH_ASKPASS":"/bin/false"}
    # subprocess.run(env=...) replaces the whole environment rather than
    # inheriting it, so without this the clone below silently drops the
    # systemd unit's HTTP(S)_PROXY -- this server cannot reach github.com
    # directly, and the clone hung for ~90s before a TLS reset the first
    # time this ran against the real network.
    environment.update({key: os.environ[key] for key in PROXY_ENV_KEYS if key in os.environ})
    environment.update({"GIT_CONFIG_COUNT":"2","GIT_CONFIG_KEY_0":"credential.helper","GIT_CONFIG_VALUE_0":"","GIT_CONFIG_KEY_1":f"http.{fixed}.extraHeader","GIT_CONFIG_VALUE_1":auth})
    def run(*command):
        result=subprocess.run(command,cwd=repo if repo.exists() else clone_root,env=environment,stdin=subprocess.DEVNULL,check=False)
        if result.returncode: raise SystemExit("fixed Git operation failed")
    run(*git,"clone","--no-local","--no-hardlinks","--",fixed,str(repo))
    overrides=("core.hooksPath","credential.helper","credential.interactive","core.fsmonitor","core.sshCommand","diff.external","protocol.allow","protocol.https.allow",f"http.{fixed}.extraHeader")
    values=("/dev/null","","never","false","","","never","always",auth)
    os.environ.update({"HOME":str(home),"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_GLOBAL":"/dev/null","GIT_ATTR_NOSYSTEM":"1","GIT_TERMINAL_PROMPT":"0","GIT_ASKPASS":"/bin/false","SSH_ASKPASS":"/bin/false","GIT_CONFIG_COUNT":str(len(overrides))})
    # ingestion.automation.core.git()'s own commit calls (used by
    # module.main()'s scheduled-run path below) pass no -c user.name/
    # user.email and have no ~/.gitconfig to fall back to under the fresh,
    # empty HOME above -- the old unsandboxed root-run mechanism silently
    # relied on root's real global gitconfig for this. These env vars are
    # git's standard identity override, picked up by every subsequent git
    # invocation in this process without needing a config file at all.
    os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "DEK Source Ingestion"
    os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "ingestion@invalid"
    os.environ["DEK_INGEST_RUN_NONCE"] = run_nonce
    os.environ["DEK_INGEST_STARTED_AT"] = _utc_text(invocation_started)
    for index,(key,value) in enumerate(zip(overrides,values)):
        os.environ[f"GIT_CONFIG_KEY_{index}"]=key; os.environ[f"GIT_CONFIG_VALUE_{index}"]=value
    if args.command == "refresh-bundle":
        # Only re-publish origin's current state to the reviewers: no source is fetched, nothing
        # is committed or pushed, no ingestion code runs and no proof record is touched.
        write_review_bundle(run, git, repo, temporary)
        return 0
    module, _core, _fetchers, _audit = load_ingestion_modules(installed)
    module.ROOT = repo
    module.APPROVAL_PATH = repo / "_" / "ingestion" / "approval.json"
    before_reports = report_snapshot(repo)
    candidate_argv = [args.command]
    if args.pre_cutover_proof:
        candidate_argv.append("--no-publication")
    result=module.main(candidate_argv)
    persist_current_report(repo, before_reports, args.proof_output,
                           run_nonce=run_nonce, started_at=invocation_started)
    if result: return result
    if args.pre_cutover_proof:
        shutil.rmtree(temporary)
        return 0
    # Publish the summary before the bundle.  The review page treats the
    # bundle replacement as completion, so once it observes completion the
    # matching counts are already durable and readable.
    write_ingest_summary(args.proof_output)
    run(*git,"add","--","source","ingestion/logs","ingestion/rough")
    changed=subprocess.run((*git,"diff","--cached","--quiet"),cwd=repo,env=environment,stdin=subprocess.DEVNULL).returncode
    plan = publication_plan(pre_cutover=args.pre_cutover_proof, changed=bool(changed))
    if "commit" in plan:
        run(*git,"-c","user.name=DEK Source Ingestion","-c","user.email=ingestion@invalid","commit","-m","chore: ingest sources")
    if "push" in plan:
        run(*git,"push","--",fixed,"HEAD:refs/heads/main")
    write_review_bundle(run, git, repo, temporary)
    # Preserve the actual pull completion independently from repository.bundle:
    # publishing also refreshes that bundle, but must not rewrite the last-pull time.
    write_ingest_summary(args.proof_output, started_at=invocation_started,
                         completed_at=datetime.now(timezone.utc))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
