"""Hardening tests for the deploy entrypoints.

* builder_entrypoint: decision_id/nonce are validated against a strict
  identifier regex before the generation directory path is composed, and any
  path-traversal / unsafe value fails closed.
* publisher_entrypoint: legacy ``.prepared-`` / ``.processed-`` staging is dead
  code that must not remain; only ``.preparing-`` is used.
"""
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from deploy.builder_entrypoint import main as builder_main
from deploy.builder_entrypoint import validate_identifier
from deploy.publisher_entrypoint import process_decision


class BuilderIdentifierValidationTests(unittest.TestCase):
    def test_valid_identifier_accepted(self):
        for value in ("decision-12345678", "aBcD_1234-xyz", "a" * 79, "0" * 8):
            self.assertEqual(validate_identifier(value, "decision_id"), value)

    def test_real_token_urlsafe_24_is_a_valid_component(self):
        value = secrets.token_urlsafe(24)
        self.assertEqual(len(value), 32)
        self.assertEqual(validate_identifier(value, "decision_id"), value)

    def test_rejects_path_traversal_and_unsafe_characters(self):
        for value in (
            "../etc/passwd", "..", "a/b", "a b", "a..b", "a" * 7, "a" * 80,
            "", "a\x00b", "a\nb", "a|b", "a$b", "a;b", "a<b", "a?b", "a*b",
            "a:b", "a\\b", ".", "..",
        ):
            with self.assertRaises(SystemExit):
                validate_identifier(value, "decision_id")

    def test_rejects_non_string_identifiers(self):
        for value in (None, 12345, ["decision-12345678"], {"x": "y"}):
            with self.assertRaises(SystemExit):
                validate_identifier(value, "decision_id")

    def test_component_boundary_guarantees_derived_generation_fits_contract(self):
        decision_id = "d" * 79
        nonce = "n" * 79
        self.assertEqual(validate_identifier(decision_id, "decision_id"), decision_id)
        self.assertEqual(validate_identifier(nonce, "nonce"), nonce)
        self.assertEqual(len(f"{decision_id}-{nonce}"), 159)
        for value in ("d" * 80, "n" * 80):
            with self.assertRaises(SystemExit):
                validate_identifier(value, "approval identity")


class BuilderFailClosedTests(unittest.TestCase):
    def _build_config(self, root, decision_id, nonce):
        key = Ed25519PrivateKey.generate()
        pub = root / "pub.pem"
        pub.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        approved = root / "approved"
        builds = root / "builds"
        approved.mkdir()
        builds.mkdir()
        pkg = approved / "pkg"
        pkg.mkdir()
        (pkg / "approval.json").write_text(
            json.dumps({"decision_id": decision_id, "nonce": nonce}),
            encoding="utf-8",
        )
        cfg = root / "builder.json"
        cfg.write_text(
            json.dumps(
                {
                    "approved_root": str(approved),
                    "build_root": str(builds),
                    "approval_public_key": str(pub),
                }
            ),
            encoding="utf-8",
        )
        return cfg, builds

    def test_decision_id_path_traversal_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, builds = self._build_config(root, "../escape", "nonce-12345678")
            self.assertEqual(builder_main(["--config", str(cfg)]), 0)
            self.assertEqual([path.name for path in builds.iterdir()], [".builder-failures"])
            self.assertEqual(len(list((builds / ".builder-failures").glob("*.json"))), 1)

    def test_nonce_path_traversal_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, builds = self._build_config(root, "decision-12345678", "a/../b")
            self.assertEqual(builder_main(["--config", str(cfg)]), 0)
            self.assertEqual([path.name for path in builds.iterdir()], [".builder-failures"])
            self.assertEqual(len(list((builds / ".builder-failures").glob("*.json"))), 1)


class PublisherDeadCodeRemovalTests(unittest.TestCase):
    def test_source_has_no_legacy_prepared_or_processed_staging(self):
        code = Path("deploy/publisher_entrypoint.py").read_text(encoding="utf-8")
        self.assertNotIn(".prepared-", code)
        self.assertNotIn(".processed-", code)
        self.assertIn(".preparing-", code)

    def test_process_decision_prepares_via_preparing_staging_only(self):
        calls = []

        class MockPublisher:
            origin = "https://github.com/chenponsh/dek.git"

            @staticmethod
            def verify_review_snapshot(bundle, commit, tree, digest):
                calls.append(("verify", bundle.name))

            def prepare_change(self, output, decision):
                calls.append(("prepare", output.name))
                output.mkdir(parents=True, exist_ok=False)
                (output / "approval.json").write_text(
                    json.dumps(
                        {
                            "decision_id": decision["decision_id"],
                            "nonce": decision["decision_id"],
                        }
                    ),
                    encoding="utf-8",
                )

            def finalize(self, package):
                (package / "release.sig").write_text("sig")

            def publish(self, package):
                calls.append(("publish", package.name))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approved = root / "approved"
            approved.mkdir()
            builds = root / "builds"
            builds.mkdir()
            archive = root / "archive"
            archive.mkdir()
            (archive / "deadbeef.bundle").write_bytes(b"bundle")
            decision = {
                "decision_id": "decision-12345678",
                "snapshot_commit": "1" * 40,
                "snapshot_tree": "2" * 40,
                "snapshot_bundle_sha256": "deadbeef",
            }
            pub = MockPublisher()
            self.assertEqual(process_decision(pub, decision, approved, builds, archive), "wait")
            self.assertIn(("prepare", ".preparing-decision-12345678"), calls)
            self.assertNotIn(("prepare", ".prepared-decision-12345678"), calls)
            self.assertFalse((approved / ".prepared-decision-12345678").exists())


if __name__ == "__main__":
    unittest.main()
