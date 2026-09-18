#!/usr/bin/python3
"""One-time bootstrap: build and sign the very first activator release
directly from an existing git ref, with no prior review decision behind it
(the content is already live; there is nothing to "approve"). Never used
again after the real review -> build -> publish -> activate pipeline is
seeded once. Deliberately bypasses BundleBuilder/ReleasePublisher, whose
approval.json/decision-bound schema has no meaning for a from-nothing seed;
this writes the simpler `"sequence" in metadata` release.json shape that
Activator._validate_metadata() and seed_release.py's active_metadata()
already accept without an approval/activation-ready signature pair.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from deploy.activator import STATIC_SUFFIXES
from deploy.release_bundle import GIT, _canonical_release, _digest, _run

SEED_NONCE = "seed-0000001"
SEED_GENERATION = "seed-genesis-0000001"


_NON_ARTIFACT_ENTRIES = {"repository.bundle", "release.json", "release.sig"}


def _validate_static_output(output: Path) -> dict[str, str]:
    required = {"site/index.html", "dek-kb.json"}
    found: set[str] = set()
    digests: dict[str, str] = {}
    for path in sorted(output.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(output).as_posix()
        if relative in _NON_ARTIFACT_ENTRIES:
            continue
        if relative != "dek-kb.json" and (not relative.startswith("site/") or path.suffix.lower() not in STATIC_SUFFIXES):
            raise SystemExit(f"unexpected build output: {relative}")
        found.add(relative)
        digests[relative] = _digest(path)
    if not required.issubset(found):
        raise SystemExit("incomplete static output")
    return digests


def build_seed_release(*, repo: Path, ref: str, output: Path, signing_key: Ed25519PrivateKey,
                       web_python: str, qa_python: str, runner=_run) -> dict:
    """Build and sign a sequence=1 release for `ref` from `repo` into `output`.

    `output` must not already exist. Returns the signed release descriptor.
    """
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")
    commit = runner((*GIT, "rev-parse", f"{ref}^{{commit}}"), cwd=repo).decode().strip()
    tree = runner((*GIT, "rev-parse", f"{commit}^{{tree}}"), cwd=repo).decode().strip()
    output.mkdir(parents=True)
    bundle = output / "repository.bundle"
    with tempfile.TemporaryDirectory(prefix="dek-seed-") as temporary:
        # Work in a disposable clone -- matching ReleasePublisher.prepare_change()
        # -- so this never leaves a stray branch or other side effect in the
        # caller's own repo, which may be a real, in-use working tree.
        clone = Path(temporary) / "clone"
        runner((*GIT, "-c", "protocol.file.allow=always", "clone", "--no-checkout", "--", str(repo), str(clone)))
        # `git bundle create <path> <bare-sha>` fails ("Refusing to create
        # empty bundle"): bundle needs a proper ref to express what's
        # included, not a bare commit -- point a branch at it first.
        runner((*GIT, "branch", "--force", "dek-seed", commit), cwd=clone)
        runner((*GIT, "bundle", "create", str(bundle), "refs/heads/dek-seed"), cwd=clone)
        bundle_sha256 = _digest(bundle)
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        archive = runner((*GIT, "archive", "--format=tar", commit), cwd=clone)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise SystemExit("unsafe git archive member")
            tar.extractall(snapshot, filter="data")
        runner((web_python, "-m", "web.site", "--vault", str(snapshot), "--output", str(output / "site")), cwd=snapshot)
        runner((qa_python, "-m", "qa.dek_qa.build_index", "--vault", str(snapshot), "--output", str(output / "dek-kb.json")), cwd=snapshot)
    artifacts = _validate_static_output(output)
    release = {
        "schema_version": 2, "sequence": 1,
        "nonce": SEED_NONCE, "generation": SEED_GENERATION, "previous_generation": None,
        "commit": commit, "tree": tree, "bundle_sha256": bundle_sha256,
        "artifacts": artifacts,
    }
    (output / "release.json").write_text(json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    (output / "release.sig").write_bytes(signing_key.sign(_canonical_release(release)))
    return release


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--web-python", default=sys.executable)
    parser.add_argument("--qa-python", default=sys.executable)
    args = parser.parse_args(argv)
    signing_key = load_pem_private_key(args.signing_key.read_bytes(), password=None)
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise SystemExit("signing key must be Ed25519")
    release = build_seed_release(
        repo=args.repo.resolve(strict=True), ref=args.ref, output=args.output,
        signing_key=signing_key, web_python=args.web_python, qa_python=args.qa_python,
    )
    print(json.dumps({"commit": release["commit"], "tree": release["tree"],
                      "bundle_sha256": release["bundle_sha256"], "generation": release["generation"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
