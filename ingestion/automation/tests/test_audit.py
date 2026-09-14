import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ingestion.automation.audit import audit_history


class AuditTests(unittest.TestCase):
    def test_audit_finds_updated_event_without_rough_and_reads_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            logs.mkdir(parents=True)
            payload = {
                "date": "2026-08-23",
                "report": {"source/CDE/example.md": {"status": "updated_with_new"}},
                "rough_created": [],
            }
            (logs / "source_ingest_20260823_0924_report.json").write_text(
                "\ufeff" + json.dumps(payload), encoding="utf-8"
            )
            result = audit_history(root)
            self.assertEqual(result["updated_events"], 1)
            self.assertEqual(result["missing_rough_events"], 1)
            self.assertEqual(result["backlog"][0]["source"], "source/CDE/example.md")

    def test_audit_accepts_legacy_list_reports_and_counts_rough(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            logs.mkdir(parents=True)
            payload = {
                "date": "2026-07-16",
                "report": [{"source": "source/上海/example.md", "status": "updated_with_new"}],
                "rough_created": ["ingestion/rough/example.md"],
            }
            (logs / "source_ingest_20260716_0930_report.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            result = audit_history(root)
            self.assertEqual(result["updated_events"], 1)
            self.assertEqual(result["missing_rough_events"], 0)

    def test_audit_preserves_all_reports_and_stably_deduplicates_legacy_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            logs.mkdir(parents=True)
            payload = {
                "date": "2026-08-01",
                "report": {"source/example.md": {"status": "updated_with_new"}},
                "rough_created": [],
            }
            for stamp in ("0931", "0936", "0939"):
                (logs / f"source_ingest_20260801_{stamp}_report.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
            result = audit_history(root)
            self.assertEqual(result["updated_events"], 3)
            self.assertEqual(result["missing_rough_events"], 1)
            self.assertEqual(
                result["backlog"][0]["reports"],
                [
                    "ingestion/logs/source_ingest_20260801_0931_report.json",
                    "ingestion/logs/source_ingest_20260801_0936_report.json",
                    "ingestion/logs/source_ingest_20260801_0939_report.json",
                ],
            )

    def test_source_item_key_keeps_same_day_source_events_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            rough = root / "ingestion" / "rough"
            logs.mkdir(parents=True)
            rough.mkdir(parents=True)
            for stamp, key in (("0931", "sha256:first"), ("0936", "sha256:second")):
                payload = {
                    "date": "2026-08-01",
                    "report": {"source/example.md": {
                        "status": "updated_with_new", "source_item_key": key,
                    }},
                    "rough_created": [],
                }
                (logs / f"source_ingest_20260801_{stamp}_report.json").write_text(json.dumps(payload), encoding="utf-8")
            (rough / "first.md").write_text(
                '---\ningested_at: 2026-08-01\nsource: "[[source/example]]"\nsource_item_key: sha256:first\n---\n',
                encoding="utf-8",
            )
            result = audit_history(root)
            self.assertEqual(result["updated_events"], 2)
            self.assertEqual(result["missing_rough_events"], 1)
            self.assertEqual(result["backlog"][0]["source_item_key"], "sha256:second")

    def test_rough_created_only_reconciles_its_mapped_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            logs.mkdir(parents=True)
            payload = {
                "date": "2026-08-01",
                "report": {
                    "source/one.md": {"status": "updated_with_new"},
                    "source/two.md": {"status": "updated_with_new"},
                },
                "rough_created": ["ingestion/rough/one.md"],
                "rough_sources": {"ingestion/rough/one.md": "source/one.md"},
            }
            (logs / "source_ingest_20260801_0931_report.json").write_text(json.dumps(payload), encoding="utf-8")
            result = audit_history(root)
            self.assertEqual(result["missing_rough_events"], 1)
            self.assertEqual(result["backlog"][0]["source"], "source/two.md")

    def test_explicit_exclusion_requires_reason_and_removes_matching_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            automation = root / "ingestion" / "automation"
            logs.mkdir(parents=True)
            automation.mkdir(parents=True)
            payload = {"date": "2026-08-01", "report": {"source/fake.md": {"status": "updated_with_new"}}}
            (logs / "source_ingest_20260801_0931_report.json").write_text(json.dumps(payload), encoding="utf-8")
            (automation / "audit_exclusions.json").write_text(json.dumps({"exclusions": [
                {"date": "2026-08-01", "source": "source/fake.md", "reason": "known synthetic fixture"}
            ]}), encoding="utf-8")
            result = audit_history(root)
            self.assertEqual(result["missing_rough_events"], 0)
            self.assertEqual(result["excluded_events"][0]["reason"], "known synthetic fixture")

    def test_malformed_payload_dates_and_exclusions_are_report_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            automation = root / "ingestion" / "automation"
            logs.mkdir(parents=True)
            automation.mkdir(parents=True)
            (logs / "source_ingest_20260801_0931_report.json").write_text("[]", encoding="utf-8")
            (logs / "source_ingest_20260802_0931_report.json").write_text(
                json.dumps({"date": "2026-99-01", "report": {}}), encoding="utf-8"
            )
            (automation / "audit_exclusions.json").write_text(
                json.dumps({"exclusions": [{"date": "2026-08-01", "source": "source/fake.md"}]}), encoding="utf-8"
            )
            result = audit_history(root)
            self.assertEqual(len(result["report_errors"]), 3)

    def test_audit_reconciles_a_later_rough_by_source_and_ingested_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            rough = root / "ingestion" / "rough"
            logs.mkdir(parents=True)
            rough.mkdir(parents=True)
            payload = {
                "date": "2026-08-23",
                "report": {"source/CDE/example.md": {"status": "updated_with_new"}},
                "rough_created": [],
            }
            (logs / "source_ingest_20260823_0924_report.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (rough / "candidate.md").write_text(
                '---\ningested_at: 2026-08-23\nsource: "[[source/CDE/example]]"\nstatus: pending_review\n---\n',
                encoding="utf-8",
            )
            result = audit_history(root)
            self.assertEqual(result["missing_rough_events"], 0)

    def test_keyed_rough_reconciles_legacy_report_event_by_date_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "ingestion" / "logs"
            rough = root / "ingestion" / "rough"
            logs.mkdir(parents=True)
            rough.mkdir(parents=True)
            payload = {
                "date": "2026-08-23",
                "report": {"source/CDE/example.md": {"status": "updated_with_new"}},
                "rough_created": [],
            }
            (logs / "source_ingest_20260823_0924_report.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (rough / "candidate.md").write_text(
                '---\ningested_at: 2026-08-23\nsource: "[[source/CDE/example]]"\nsource_item_key: sha256:deadbeef\nstatus: pending_review\n---\n',
                encoding="utf-8",
            )
            result = audit_history(root)
            self.assertEqual(result["missing_rough_events"], 0)

    def test_stale_pending_uses_ingested_at_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rough = root / "ingestion" / "rough"
            rough.mkdir(parents=True)
            candidate = rough / "candidate.md"
            candidate.write_text(
                '---\ningested_at: 2026-08-01\nsource: "[[source/example]]"\nstatus: pending_review\n---\n',
                encoding="utf-8",
            )
            result = audit_history(root, today=date(2026, 9, 13))
            self.assertEqual(result["stale_pending_over_7_days"], ["ingestion/rough/candidate.md"])

    def test_audit_requires_evidence_for_terminal_rough_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rough = root / "ingestion" / "rough"
            rough.mkdir(parents=True)
            (rough / "promoted.md").write_text(
                "---\nstatus: promoted\nwiki_target:\nreviewed_at: 2026-09-14\n---\n",
                encoding="utf-8",
            )
            (rough / "rejected.md").write_text(
                "---\nstatus: rejected\nrejection_reason:\nreviewed_at: 2026-09-14\n---\n",
                encoding="utf-8",
            )
            result = audit_history(root)
            self.assertEqual(len(result["rough_lifecycle_errors"]), 2)
            self.assertIn("wiki_target", result["rough_lifecycle_errors"][0]["reason"])
            self.assertIn("rejection_reason", result["rough_lifecycle_errors"][1]["reason"])

    def test_promoted_rough_accepts_a_wiki_target_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rough = root / "ingestion" / "rough"
            rough.mkdir(parents=True)
            (rough / "promoted.md").write_text(
                '---\nstatus: promoted\nwiki_target:\n  - "[[wiki/example]]"\nreviewed_at: 2026-09-14\n---\n',
                encoding="utf-8",
            )
            self.assertEqual(audit_history(root)["rough_lifecycle_errors"], [])