from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deploy.readiness import (
    MARKER_TTL,
    ReadinessError,
    _marker_hmac,
    marker_payload,
    validate_marker,
    write_marker,
)


CONFIG = {
    "review_origin": "https://review.example",
    "oauth_callback": "https://review.example/auth/callback",
    "reviewer_ids": ["enterprise-approved"],
    "expected_addresses": ["192.0.2.10"],
    "readiness_url": "https://review.example/__ready",
}
SECRET = b"readiness-marker-lifetime-secret-32bytes"


class MarkerConsumptionLifetimeTests(unittest.TestCase):
    BASE = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def _run_cli(config: Path, key: Path, marker: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "deploy/readiness.py",
                "--config", str(config),
                "--hmac-key", str(key),
                "--validate-marker",
                "--marker", str(marker),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_marker(self, target: Path, *, clock) -> None:
        write_marker(
            target, CONFIG, SECRET, clock=clock,
            confirmed_authorized_login=True, confirmed_unauthorized_login=True,
        )
        os.chmod(target, 0o444)

    def test_cli_rejects_marker_issued_in_2000(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "readiness.json"
            key = root / "marker-hmac-key"
            marker = root / "automation-ready"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")
            key.write_bytes(SECRET)
            os.chmod(key, 0o400)
            self._write_marker(marker, clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc))
            result = self._run_cli(config, key, marker)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("expired", result.stderr)

    def test_marker_is_authenticated_and_expires_after_fixed_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "automation-ready"
            self._write_marker(marker, clock=lambda: self.BASE)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(
                set(payload),
                {"schema", "status", "config_hmac", "issued_at", "expires_at",
                 "confirmed_authorized_login", "confirmed_unauthorized_login", "marker_hmac"},
            )
            self.assertTrue(payload["confirmed_authorized_login"])
            self.assertTrue(payload["confirmed_unauthorized_login"])
            self.assertEqual(
                datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00")),
                MARKER_TTL,
            )
            validate_marker(marker, CONFIG, SECRET, clock=lambda: self.BASE + MARKER_TTL - timedelta(seconds=1))
            with self.assertRaisesRegex(ReadinessError, "expired"):
                validate_marker(marker, CONFIG, SECRET, clock=lambda: self.BASE + MARKER_TTL)

    def test_consumer_rejects_future_issued_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "automation-ready"
            self._write_marker(marker, clock=lambda: self.BASE)
            with self.assertRaisesRegex(ReadinessError, "future"):
                validate_marker(marker, CONFIG, SECRET, clock=lambda: self.BASE - timedelta(minutes=1))

    def test_consumer_rejects_a_tampered_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "automation-ready"
            self._write_marker(marker, clock=lambda: self.BASE)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["expires_at"] = "2099-01-01T00:00:00Z"
            marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.chmod(marker, 0o444)
            with self.assertRaisesRegex(ReadinessError, "authentication"):
                validate_marker(marker, CONFIG, SECRET, clock=lambda: self.BASE + timedelta(minutes=1))

    def test_consumer_rejects_a_marker_with_an_unconfirmed_login_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "automation-ready"
            payload = marker_payload(
                CONFIG, SECRET, clock=lambda: self.BASE,
                confirmed_authorized_login=True, confirmed_unauthorized_login=True,
            )
            payload["confirmed_unauthorized_login"] = False
            unsigned = {key: value for key, value in payload.items() if key != "marker_hmac"}
            payload["marker_hmac"] = _marker_hmac(unsigned, SECRET)
            marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.chmod(marker, 0o444)
            with self.assertRaisesRegex(ReadinessError, "invalid readiness marker"):
                validate_marker(marker, CONFIG, SECRET, clock=lambda: self.BASE + timedelta(minutes=1))

    def test_marker_payload_refuses_to_build_without_both_confirmations(self):
        with self.assertRaisesRegex(ReadinessError, "confirmed"):
            marker_payload(
                CONFIG, SECRET, clock=lambda: self.BASE,
                confirmed_authorized_login=True, confirmed_unauthorized_login=False,
            )


if __name__ == "__main__":
    unittest.main()
