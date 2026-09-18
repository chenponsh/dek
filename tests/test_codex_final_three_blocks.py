from __future__ import annotations

import tempfile
import unittest
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import yaml


class SystemdEnvironmentByteEquivalenceTests(unittest.TestCase):
    BASE = (
        b"DINGTALK_CLIENT_ID=ding-client-918273\n"
        b"DINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\n"
        b"DINGTALK_AGENT_ID=123456\n"
        b"DINGTALK_ALLOWED_USERS=*\n"
        b"DINGTALK_ALLOW_ALL_USERS=true\n"
    )

    def _parse(self, raw: bytes):
        from deploy.credential_gate import _environment

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment"
            path.write_bytes(raw)
            return _environment(path, "qa")

    def test_plain_subset_preserves_the_exact_bytes_systemd_will_deliver(self):
        values = self._parse(b"# full-line comment\n" + self.BASE)
        self.assertEqual(values["DINGTALK_CLIENT_SECRET"], b"Q7vN2xL9pR4mT8kW6sH3")

    def test_gate_compares_parsed_bytes_to_the_same_service_process_environment(self):
        from deploy.credential_gate import CredentialError, validate_process_environment

        process = {
            line.split(b"=", 1)[0].decode("ascii"): line.split(b"=", 1)[1].decode("ascii")
            for line in self.BASE.splitlines()
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment"
            path.write_bytes(self.BASE)
            validate_process_environment(path, "qa", process)
            process["DINGTALK_CLIENT_SECRET"] += "-changed"
            with self.assertRaises(CredentialError) as raised:
                validate_process_environment(path, "qa", process)
            self.assertNotIn(process["DINGTALK_CLIENT_SECRET"], str(raised.exception))

    def test_rejects_systemd_metasyntax_including_backslash_quote_bypass_without_echo(self):
        from deploy.credential_gate import CredentialError

        variants = (
            b'DINGTALK_CLIENT_SECRET=R7vN2xL9pR4mT8kW6sH3\\\\ "qa"\n',
            b'DINGTALK_CLIENT_SECRET="R7vN2xL9pR4mT8kW6sH3"\n',
            b"DINGTALK_CLIENT_SECRET='R7vN2xL9pR4mT8kW6sH3'\n",
            b"DINGTALK_CLIENT_SECRET=R7vN2xL9pR4mT8kW6sH3\\\ncontinued\n",
            b"export DINGTALK_CLIENT_SECRET=R7vN2xL9pR4mT8kW6sH3\n",
        )
        baseline = self.BASE.splitlines(keepends=True)
        for replacement in variants:
            with self.subTest(replacement=replacement[:12]):
                raw = b"".join(
                    replacement if line.startswith(b"DINGTALK_CLIENT_SECRET=") else line
                    for line in baseline
                )
                with self.assertRaises(CredentialError) as raised:
                    self._parse(raw)
                self.assertNotIn("R7vN2xL9pR4mT8kW6sH3", str(raised.exception))

    def test_rejects_duplicate_and_ambiguous_comment_or_whitespace_forms(self):
        from deploy.credential_gate import CredentialError

        variants = (
            self.BASE + b"DINGTALK_AGENT_ID=654321\n",
            self.BASE.replace(b"DINGTALK_AGENT_ID=", b" DINGTALK_AGENT_ID="),
            self.BASE.replace(b"DINGTALK_AGENT_ID=123456", b"DINGTALK_AGENT_ID=123456 # comment"),
            b"; systemd comment\n" + self.BASE,
            b" # indented systemd comment\n" + self.BASE,
        )
        for raw in variants:
            with self.subTest(prefix=raw[:20]):
                with self.assertRaises(CredentialError):
                    self._parse(raw)


class A6PreCutoverAcceptanceOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        cls.a5 = text[text.index("## Checkpoint A5b"):text.index("## Checkpoint A6")]
        cls.a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]

    def test_qa_candidates_are_built_only_under_the_isolated_stage_root(self):
        self.assertIn("STAGE_ROOT=/var/lib/dek-stage-a/$PACKAGE_SHA256", self.a5)
        self.assertIn('QA_CANDIDATE="$STAGE_ROOT/qa/venv"', self.a5)
        self.assertIn('QA_PROFILE="$STAGE_ROOT/qa/profile"', self.a5)
        self.assertNotIn("-m venv /var/lib/dek-qa/venvs/$PACKAGE_SHA256", self.a5)
        self.assertNotIn('QA_PROFILE="/var/lib/dek-qa/hermes/profile-versions/', self.a5)

    def test_all_candidate_gates_precede_the_explicit_live_cutover_boundary(self):
        boundary = self.a6.index("# LIVE CUTOVER BEGINS")
        before = self.a6[:boundary]
        after = self.a6[boundary:]
        gate = before.index("credential_gate.py")
        hermes = before.index("--unit=dek-qa-a6-tool-acceptance")
        ingestion = before.index("--unit=dek-source-ingest-a6-candidate")
        self.assertLess(gate, hermes)
        self.assertLess(hermes, ingestion)
        self.assertIn('STAGED_REPORT="$STAGE_ROOT/ingestion/proofs/staged-proof-report.json"', before)
        self.assertIn("EnvironmentFile=/var/lib/dek-qa/secrets/environment", before)
        self.assertIn("--environment-file /var/lib/dek-qa/secrets/environment", before)
        self.assertNotIn("systemctl stop dek-source-ingest.timer", before)
        self.assertNotIn("--cutover-source-ingest", before)
        self.assertNotIn("systemctl restart dek-web.service dek-qa.service", before)
        self.assertIn("a6_cutover.py apply", after)
        self.assertIn("a6_cutover.py recover", after)
        self.assertNotIn("--cutover-source-ingest", after)
        self.assertNotIn('mv "$QA_CANDIDATE"', after)
        self.assertNotIn('mv "$QA_PROFILE"', after)

    def test_staged_hermes_validation_rebinds_only_the_two_execution_paths(self):
        from deploy.qa_profile import validate_staged_with_hermes

        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            config = {
                "platforms": {"dingtalk": {"enabled": True, "extra": {
                    "allowed_users": ["*"], "allowed_chats": ["chat"], "require_mention": True,
                }}},
                "mcp_servers": {"dek_kb": {
                    "command": "/var/lib/dek-qa/venvs/" + "a" * 64 + "/bin/python",
                    "args": ["-m", "qa.dek_qa.mcp_server"],
                    "env": {"PYTHONPATH": "/opt/dek-qa/app"},
                }},
                "sentinel": {"must": "remain"},
            }
            path = profile / "config.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            observed = {}

            def capture(candidate_dir, expected):
                observed["expected"] = expected
                observed["parsed"] = yaml.safe_load((candidate_dir / "config.yaml").read_text())

            with patch("deploy.qa_profile.validate_with_hermes", side_effect=capture):
                validate_staged_with_hermes(path, "/stage/venv/bin/python", "/stage/package")

            rebound = observed["expected"]
            self.assertEqual(rebound, observed["parsed"])
            self.assertEqual("/stage/venv/bin/python", rebound["mcp_servers"]["dek_kb"]["command"])
            self.assertEqual({"PYTHONPATH": "/stage/package"}, rebound["mcp_servers"]["dek_kb"]["env"])
            self.assertEqual(config["sentinel"], rebound["sentinel"])
            self.assertEqual(config, yaml.safe_load(path.read_text()))


class InstallRootConfinementTests(unittest.TestCase):
    DIGEST = "d" * 64

    def _inputs(self, base: Path):
        from deploy.install_components import SERVICES

        package = base / "package"
        payload = package / "deploy" / "payload.py"
        payload.parent.mkdir(parents=True)
        payload.write_text("candidate", encoding="utf-8")
        manifest = package / "PACKAGE.sha256"
        manifest.write_text(
            f"{hashlib.sha256(payload.read_bytes()).hexdigest()}  deploy/payload.py\n",
            encoding="utf-8",
        )
        roots = {}
        for service in SERVICES:
            root = base / "roots" / service
            root.mkdir(parents=True)
            roots[service] = root
        journal = base / "journal"
        journal.mkdir(mode=0o700)
        return package, manifest, roots, journal

    def _install_one(self, package, manifest, roots, journal, **kwargs):
        from deploy.install_components import install_versioned_components

        kwargs.setdefault("test_only_allow_unsafe_ancestors", {package.parent.parent})
        return install_versioned_components(
            package, manifest, roots, self.DIGEST,
            selected_services={"dek-activator"}, journal_dir=journal, **kwargs,
        )

    def test_rejects_approved_root_symlink_without_writing_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package, manifest, roots, journal = self._inputs(base)
            approved = roots["dek-activator"]
            approved.rmdir()
            outside = base / "outside"
            outside.mkdir()
            approved.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(RuntimeError):
                self._install_one(package, manifest, roots, journal)
            self.assertEqual([], list(outside.iterdir()))

    def test_rejects_symlink_ancestor_and_unsafe_root_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package, manifest, roots, journal = self._inputs(base)
            approved = roots["dek-activator"]
            os.chmod(approved, 0o777)
            with self.assertRaises(RuntimeError):
                self._install_one(package, manifest, roots, journal)
            os.chmod(approved, 0o755)

            real_parent = base / "real-parent"
            real_parent.mkdir()
            escaped = real_parent / "dek-activator"
            escaped.mkdir()
            alias = base / "alias-parent"
            alias.symlink_to(real_parent, target_is_directory=True)
            roots["dek-activator"] = alias / "dek-activator"
            with self.assertRaises(RuntimeError):
                self._install_one(package, manifest, roots, journal)
            self.assertEqual([], list(escaped.iterdir()))

    def test_rejects_non_directory_and_non_root_owned_approved_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package, manifest, roots, journal = self._inputs(base)
            approved = roots["dek-activator"]
            os.chown(approved, 65534, 65534)
            with self.assertRaises(RuntimeError):
                self._install_one(package, manifest, roots, journal)
            os.chown(approved, 0, 0)
            approved.rmdir()
            approved.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self._install_one(package, manifest, roots, journal)

    def test_path_replacement_after_validation_cannot_redirect_switch_or_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package, manifest, roots, journal = self._inputs(base)
            approved = roots["dek-activator"]
            outside = base / "outside"
            outside.mkdir()
            parked = base / "parked-approved-root"

            def replace_root(_service, _index):
                approved.rename(parked)
                approved.symlink_to(outside, target_is_directory=True)

            self._install_one(package, manifest, roots, journal, before_switch=replace_root)
            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse((outside / "current").exists())
            self.assertFalse((outside / "install-manifest.expected").exists())
            self.assertEqual(f"versions/{self.DIGEST}", os.readlink(parked / "current"))

    def test_failure_rollback_uses_the_same_pinned_root_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package, manifest, roots, journal = self._inputs(base)
            approved = roots["dek-activator"]
            outside = base / "outside"; outside.mkdir()
            parked = base / "parked-approved-root"

            def replace_then_fail(step):
                if step == "dek-activator:manifest-expected":
                    approved.rename(parked)
                    approved.symlink_to(outside, target_is_directory=True)
                    raise RuntimeError("rollback boundary probe")

            with self.assertRaisesRegex(RuntimeError, "rollback boundary probe"):
                self._install_one(package, manifest, roots, journal, after_step=replace_then_fail)
            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse((parked / "install-manifest.expected").exists())
            self.assertFalse((journal / "install-components.json").exists())


if __name__ == "__main__":
    unittest.main()
