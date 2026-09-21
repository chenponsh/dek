#!/usr/bin/python3
"""Fixed one-shot runner for approved bundles; no network or credentials."""
import argparse
import json
import os
import shutil
import sys
import hashlib
import stat
import traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from deploy.activator import fsync_tree
from deploy.release_bundle import APPROVAL_ID_PATTERN, BundleBuilder, approval_generation
from deploy.fsutil import atomic_write_json

IDENTIFIER = APPROVAL_ID_PATTERN


def validate_identifier(value, field):
    """Fail closed unless the value is a plain identifier safe to join into a path."""
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise SystemExit(f"invalid {field}: {value!r}")
    return value


def make_activator_readable(root: Path) -> None:
    """Publish complete bytes as traversable/read-only to non-writers.

    The inherited build-write group retains publisher write access; other users get
    only read/traverse.  Apply before the atomic rename, never to a visible partial.

    Deliberately 0775/0664, not 02775: dek-builder.service's own sandbox sets
    RestrictSUIDSGID=true, which blocks any chmod() call whose mode argument
    carries S_ISGID -- even a no-op chmod to a value the file already has.
    The setgid bit doesn't need setting here anyway: root (the setgid dirs
    under builds/) is provisioned setgid once, outside this sandbox, and
    every directory this loop touches was created underneath it during this
    same build, so it already inherited both the group and the setgid bit at
    mkdir() time. chmod never touches ownership, only the mode bits actually
    passed, so dropping S_ISGID from the request here does not change the
    group that was already set -- it just stops re-asserting a bit that's
    already correct and that this sandbox cannot legally re-assert anyway.
    """
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, 0o775 if path.is_dir() else 0o664)
    os.chmod(root, 0o775)


def build_atomically(builder, package: Path, target: Path) -> None:
    staging=target.parent/(".staging-"+target.name)
    if staging.exists(): shutil.rmtree(staging)
    try:
        builder.build(package,staging)
        make_activator_readable(staging)
        fsync_tree(staging)
        os.replace(staging,target)
        descriptor=os.open(target.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    except (Exception, SystemExit):
        if staging.exists(): shutil.rmtree(staging)
        raise


def _bounded_approval(path: Path) -> dict:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > 65536:
            raise ValueError("unsafe approval package")
        chunks=[]; total=0
        while True:
            chunk=os.read(descriptor,min(65536,65537-total))
            if not chunk: break
            chunks.append(chunk); total+=len(chunk)
            if total>65536: raise ValueError("oversized approval package")
        raw=b"".join(chunks)
        if len(raw) != details.st_size:
            raise ValueError("approval package changed during read")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("approval is not an object")
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)


MAX_BUILD_ATTEMPTS = 3


def _failure_path(failures: Path, package: Path) -> Path:
    return failures / (hashlib.sha256(package.name.encode("utf-8", "surrogateescape")).hexdigest() + ".json")


def _read_failure(failures: Path, package: Path) -> dict:
    try:
        value = json.loads(_failure_path(failures, package).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_failure(failures: Path, package: Path, exc: BaseException) -> None:
    attempts = int(_read_failure(failures, package).get("attempts", 0) or 0) + 1
    value = {"status": "isolated", "package": package.name, "error_type": type(exc).__name__,
             "attempts": attempts, "last_error": str(exc)[-600:]}
    atomic_write_json(_failure_path(failures, package), value, mode=0o600, ensure_parent_mode=0o700)


def _clear_failure(failures: Path, package: Path) -> None:
    try:
        _failure_path(failures, package).unlink()
    except OSError:
        pass


def process_packages(builder, approved: Path, builds: Path, failures: Path) -> None:
    """Build every approved package that has no build yet.

    A package that fails is recorded and the later ones still run. One that has failed
    MAX_BUILD_ATTEMPTS times is left alone (one line in the log instead of a full rebuild
    and traceback on every publish, which buried the real failures); delete its record
    under builds/.builder-failures to try it again."""
    for package in sorted(approved.iterdir()):
        if not package.is_dir() or package.is_symlink() or package.name.startswith("."):
            continue
        try:
            approval = _bounded_approval(package / "approval.json")
            decision_id = validate_identifier(approval["decision_id"], "decision_id")
            nonce = validate_identifier(approval["nonce"], "nonce")
            generation = approval_generation(decision_id, nonce)
            target = builds / generation
            if not target.exists():
                previous = _read_failure(failures, package)
                if int(previous.get("attempts", 0) or 0) >= MAX_BUILD_ATTEMPTS:
                    print(f"DEK build skipped package={package.name} failed {previous['attempts']} times; "
                          f"last error: {str(previous.get('last_error', ''))[-200:]!r}", file=sys.stderr)
                    continue
                build_atomically(builder, package, target)
                _clear_failure(failures, package)
        except (Exception, SystemExit) as exc:
            print(f"DEK build failed package={package.name}: {str(exc)[-1500:]}", file=sys.stderr)
            _record_failure(failures, package, exc)


def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,required=True); args=parser.parse_args(argv)
    value=json.loads(args.config.read_text(encoding="utf-8"))
    if set(value)!={"approved_root","build_root","approval_public_key"}: raise SystemExit("invalid builder config")
    key=load_pem_public_key(Path(value["approval_public_key"]).read_bytes())
    builder=BundleBuilder(key)
    approved=Path(value["approved_root"]).resolve(strict=True); builds=Path(value["build_root"]).resolve(strict=True)
    process_packages(builder, approved, builds, builds/".builder-failures")
    return 0

if __name__ == "__main__": raise SystemExit(main())
