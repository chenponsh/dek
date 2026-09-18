import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.seed_current_release import SEED_GENERATION, SEED_NONCE, build_seed_release


GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def _init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, env=GIT_ENV, check=True)
    (root / "wiki").mkdir()
    (root / "wiki" / "example.md").write_text("# Example\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, env=GIT_ENV, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "seed content"],
                   cwd=root, env=GIT_ENV, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _fake_runner(command, *, cwd=None, **_kwargs):
    """Stand in for real git plumbing (subprocess) and the two content
    builders (web.site / qa.dek_qa.build_index), matching the injectable
    fixed_runner pattern already used for BundleBuilder in
    tests/test_autopublish_state_machine.py."""
    if "web.site" in command:
        output = Path(command[command.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "index.html").write_text("<html></html>", encoding="utf-8")
        return b""
    if "qa.dek_qa.build_index" in command:
        output = Path(command[command.index("--output") + 1])
        output.write_text('{"version":4,"documents":[]}', encoding="utf-8")
        return b""
    return subprocess.run(command, cwd=cwd, env=GIT_ENV, check=True, capture_output=True).stdout


class BuildSeedReleaseTests(unittest.TestCase):
    def test_build_seed_release_produces_a_verifiable_sequence_one_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"; repo.mkdir()
            commit = _init_repo(repo)
            tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, env=GIT_ENV,
                                  check=True, capture_output=True, text=True).stdout.strip()
            key = Ed25519PrivateKey.generate()
            output = root / "release"
            release = build_seed_release(
                repo=repo, ref="HEAD", output=output, signing_key=key,
                web_python="fake-web-python", qa_python="fake-qa-python", runner=_fake_runner,
            )
            self.assertEqual(release["schema_version"], 2)
            self.assertEqual(release["sequence"], 1)
            self.assertEqual(release["nonce"], SEED_NONCE)
            self.assertEqual(release["generation"], SEED_GENERATION)
            self.assertIsNone(release["previous_generation"])
            self.assertEqual(release["commit"], commit)
            self.assertEqual(release["tree"], tree)
            self.assertEqual(set(release["artifacts"]), {"site/index.html", "dek-kb.json"})
            self.assertTrue((output / "repository.bundle").is_file())

            on_disk = json.loads((output / "release.json").read_text())
            self.assertEqual(on_disk, release)
            key.public_key().verify(
                (output / "release.sig").read_bytes(),
                b"dek-final-release-v1\0" + json.dumps(release, sort_keys=True, separators=(",", ":")).encode(),
            )

    def test_build_seed_release_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"; repo.mkdir()
            _init_repo(repo)
            output = root / "release"; output.mkdir()
            with self.assertRaisesRegex(SystemExit, "refusing to overwrite"):
                build_seed_release(
                    repo=repo, ref="HEAD", output=output, signing_key=Ed25519PrivateKey.generate(),
                    web_python="x", qa_python="x", runner=_fake_runner,
                )


class SeedReleaseInstallerAcceptanceTests(unittest.TestCase):
    def test_real_installer_accepts_the_built_seed_release_end_to_end(self):
        """The actual production consumer (deploy/seed_release.py, unmodified)
        must accept build_seed_release()'s output without any special-casing --
        this is the real integration point that matters, not just the schema
        in isolation."""
        from deploy.activator import ActivatorConfig
        from deploy.seed_release import main as seed_release_main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"; repo.mkdir()
            _init_repo(repo)
            key = Ed25519PrivateKey.generate()
            output = root / "release"
            release = build_seed_release(
                repo=repo, ref="HEAD", output=output, signing_key=key,
                web_python="fake-web-python", qa_python="fake-qa-python", runner=_fake_runner,
            )
            public_key_path = root / "approval-public.pem"
            public_key_path.write_bytes(key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

            config = ActivatorConfig.under(root / "activate"); config.prepare()
            self.assertEqual(seed_release_main([
                "--release", str(output), "--public-key", str(public_key_path),
                "--releases", str(config.releases), "--active", str(config.active),
                "--expected-commit", release["commit"], "--expected-tree", release["tree"],
                "--expected-bundle-sha256", release["bundle_sha256"],
            ]), 0)

            active = json.loads(config.active.read_text())
            self.assertEqual(active["generation"], SEED_GENERATION)
            self.assertEqual(active["sequence"], 1)
            self.assertTrue((config.releases / SEED_GENERATION / "site" / "index.html").is_file())
            self.assertTrue((config.releases / SEED_GENERATION / "release.lock").exists())

            # Re-running with the identical inputs must be idempotent, not fail.
            self.assertEqual(seed_release_main([
                "--release", str(output), "--public-key", str(public_key_path),
                "--releases", str(config.releases), "--active", str(config.active),
                "--expected-commit", release["commit"], "--expected-tree", release["tree"],
                "--expected-bundle-sha256", release["bundle_sha256"],
            ]), 0)


if __name__ == "__main__":
    unittest.main()
