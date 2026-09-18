import re
import unittest
from pathlib import Path


RUNBOOK = Path(__file__).resolve().parents[1] / "deploy" / "PRODUCTION_ROLLOUT.md"


def checkpoint(text: str, name: str, next_heading: str) -> str:
    match = re.search(
        rf"^## Checkpoint {re.escape(name)}\b.*?(?=^## (?:Checkpoint )?{re.escape(next_heading)}\b)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing Checkpoint {name} before {next_heading}")
    return match.group(0)


class StageAExactInstallContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RUNBOOK.read_text(encoding="utf-8")

    def test_a3_installs_an_exact_digest_addressed_tree_and_atomically_switches_current(self):
        a3 = checkpoint(self.text, "A3", "A4")
        self.assertNotRegex(a3, r"(?m)^\s*cp\s+-a\b")
        for token in (
            "PACKAGE_SHA256",
            "/versions/$PACKAGE_SHA256",
            "install-manifest.expected",
            "install-manifest.actual",
            "digest-validates all selected version trees",
            "ln -s",
            "mv -Tf",
            "/current",
            "previous",
            "legacy-app.before-versioned",
            "install_components.py",
            "restores every entered component's original `current`, `app`, `previous`",
            "does not switch `dek-source-ingest`",
            "install-components.json",
        ):
            self.assertIn(token, a3)

    def test_a4_does_not_stop_or_disable_the_existing_ingestion_timer(self):
        a4 = checkpoint(self.text, "A4", "A5")
        self.assertNotRegex(
            a4,
            r"systemctl\s+(?:disable|stop|mask)(?:\s+--now)?[^\n]*dek-source-ingest\.timer",
        )
        self.assertNotRegex(
            a4,
            r"systemctl\s+disable\s+--now[^\n]*dek-source-ingest\.timer",
        )

    def test_a6_requires_evidence_before_ingestion_cutover_and_documents_recovery(self):
        a6 = checkpoint(self.text, "A6", "Exact manual rollback")
        old_evidence = a6.find("old.timer.contract")
        cutover = a6.find("a6_cutover.py apply")
        recovery = a6.find("a6_cutover.py recover")
        new_start = a6.find("systemctl start dek-source-ingest.service")
        evidence = a6.find("new.service.status")
        self.assertGreaterEqual(old_evidence, 0)
        self.assertGreater(cutover, old_evidence)
        self.assertGreater(recovery, cutover)
        self.assertGreater(new_start, cutover)
        self.assertGreater(evidence, new_start)
        for token in (
            "old.timer.contract",
            "systemctl show dek-source-ingest.timer",
            "systemctl status dek-source-ingest.service",
            "one-command recovery",
            "a6_cutover.py apply",
            "a6_cutover.py recover",
            "If the new ingestion proof fails",
        ):
            self.assertIn(token, a6)

    def test_a6_uses_ungated_staged_proof_then_marker_before_formal_ingestion(self):
        a6 = checkpoint(self.text, "A6", "Exact manual rollback")
        staged_path = a6.find('STAGED_REPORT="$STAGE_ROOT/ingestion/proofs/staged-proof-report.json"')
        staged = a6.find("--unit=dek-source-ingest-a6-candidate")
        staged_report = a6.find('python3 - "$STAGED_REPORT"', staged)
        promoted = a6.find("--write-marker /var/lib/dek-readiness/automation-ready --confirm-authorized-login --confirm-unauthorized-login")
        validated = a6.find("--validate-marker --marker /var/lib/dek-readiness/automation-ready")
        formal_path = a6.find("FINAL_REPORT=/var/lib/dek-source-ingest/proofs/latest-report.json")
        formal = a6.find("systemctl start dek-source-ingest.service")
        formal_report = a6.find('python3 - "$FINAL_REPORT"', formal)
        timer = a6.find("systemctl enable --now dek-source-ingest.timer")
        for position in (staged_path, staged, staged_report, promoted, validated, formal_path, formal, formal_report, timer):
            self.assertGreaterEqual(position, 0)
        self.assertLess(staged_path, staged)
        self.assertLess(staged, staged_report)
        self.assertLess(staged_report, promoted)
        self.assertLess(promoted, validated)
        self.assertLess(validated, formal_path)
        self.assertLess(validated, formal)
        self.assertLess(formal_path, formal)
        self.assertLess(formal, formal_report)
        self.assertLess(formal_report, timer)

        proof_unit = Path("deploy/systemd/dek-source-ingest-proof.service").read_text(encoding="utf-8")
        production_unit = Path("deploy/systemd/dek-source-ingest.service").read_text(encoding="utf-8")
        self.assertNotIn("ExecCondition=", proof_unit)
        self.assertIn("ExecCondition=", production_unit)
        self.assertNotIn("[Install]", proof_unit)

    def test_a6_failed_activity_contract_requires_manual_rollback_without_recovery_mutation(self):
        a6 = checkpoint(self.text, "A6", "Exact manual rollback")
        rollback = self.text[self.text.index("## Exact manual rollback"):]
        for token in (
            "ActiveState=failed or SubState=failed",
            "reset-failed + start",
            "Exact manual rollback required",
            "without any further unit, link, daemon-reload, or service mutation",
        ):
            self.assertIn(token, a6 + rollback)

    def test_proof_unit_is_non_enableable_and_keeps_the_production_sandbox(self):
        def sections(path: str) -> dict[str, list[str]]:
            result: dict[str, list[str]] = {}
            current = ""
            for raw in Path(path).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current = line
                    result[current] = []
                else:
                    result[current].append(line)
            return result

        proof = sections("deploy/systemd/dek-source-ingest-proof.service")
        production = sections("deploy/systemd/dek-source-ingest.service")
        self.assertNotIn("[Install]", proof)
        self.assertFalse(any(
            line.startswith(("WantedBy=", "RequiredBy=", "Alias=", "Also="))
            for lines in proof.values() for line in lines
        ))

        self.assertEqual(
            [line for line in proof["[Unit]"] if not line.startswith("Description=")],
            [line for line in production["[Unit]"] if not line.startswith("Description=")],
        )
        ignored = ("ExecCondition=", "ExecStart=", "SyslogIdentifier=")
        self.assertEqual(
            [line for line in proof["[Service]"] if not line.startswith(ignored)],
            [line for line in production["[Service]"] if not line.startswith(ignored)],
        )
        proof_start = next(line for line in proof["[Service]"] if line.startswith("ExecStart="))
        production_start = next(line for line in production["[Service]"] if line.startswith("ExecStart="))
        self.assertEqual(
            proof_start.replace("staged-proof-report.json", "latest-report.json").replace(
                "staged-proof-report.expected.json", "latest-report.expected.json"
            ).replace(" --pre-cutover-proof", ""),
            production_start,
        )
        self.assertIn("--pre-cutover-proof", proof_start)
        self.assertNotIn("--pre-cutover-proof", production_start)
        self.assertFalse(any(line.startswith("ExecCondition=") for line in proof["[Service]"]))
        self.assertTrue(any(line.startswith("ExecCondition=+") for line in production["[Service]"]))


if __name__ == "__main__":
    unittest.main()
