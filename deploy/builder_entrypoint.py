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
    """
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, 0o2775 if path.is_dir() else 0o664)
    os.chmod(root, 0o2775)


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


def _record_failure(failures: Path, package: Path, exc: BaseException) -> None:
    digest = hashlib.sha256(package.name.encode("utf-8", "surrogateescape")).hexdigest()
    target = failures / (digest + ".json")
    value = {"status": "isolated", "package": package.name, "error_type": type(exc).__name__}
    atomic_write_json(target, value, mode=0o600, ensure_parent_mode=0o700)


def process_packages(builder, approved: Path, builds: Path, failures: Path) -> None:
    """Isolate a malformed package durably and continue with later siblings."""
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
                build_atomically(builder, package, target)
        except (Exception, SystemExit) as exc:
            traceback.print_exc(file=sys.stderr)
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
