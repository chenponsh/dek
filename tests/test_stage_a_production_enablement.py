import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from deploy.readiness import ReadinessError, check, marker_payload, validate_configuration, write_marker


VALID = {
    "review_origin": "https://review.example",
    "oauth_callback": "https://review.example/auth/callback",
    "reviewer_ids": ["enterprise-123"],
    "expected_addresses": ["192.0.2.10"],
    "readiness_url": "https://review.example/__ready",
}
SECRET = b"test-readiness-hmac-key-material-32"


class ReadinessBindingTests(unittest.TestCase):
    def test_online_readiness_rejects_an_http_redirect(self):
        value = {
            "review_origin": "https://regkb.example",
            "oauth_callback": "https://regkb.example/review/auth/callback",
            "reviewer_ids": ["reviewer"],
            "expected_addresses": ["192.0.2.10"],
            "readiness_url": "https://regkb.example/review/__ready",
        }

        raw = mock.Mock()
        tls = mock.Mock()
        tls.getpeername.return_value = ("192.0.2.10", 443)
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        response = mock.Mock(status=302)
        response.read.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("deploy.readiness.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.0.2.10", 443))]), \
             mock.patch("deploy.readiness.socket.create_connection", return_value=raw) as create_connection, \
             mock.patch("deploy.readiness.ssl.create_default_context", return_value=context), \
             mock.patch("deploy.readiness.http.client.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(ReadinessError, "redirect"):
                check(value)
        create_connection.assert_called_once_with(("192.0.2.10", 443), timeout=5)
        connection.request.assert_called_once()

    def test_online_readiness_request_is_pinned_to_the_validated_address(self):
        raw = mock.Mock()
        tls = mock.Mock()
        tls.getpeername.return_value = ("192.0.2.10", 443)
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        response = mock.Mock(status=200)
        response.read.return_value = b'{"status":"ready","oauth_callback":"https://review.example/auth/callback"}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("deploy.readiness.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.0.2.10", 443))]) as resolve, \
             mock.patch("deploy.readiness.socket.create_connection", return_value=raw) as create_connection, \
             mock.patch("deploy.readiness.ssl.create_default_context", return_value=context), \
             mock.patch("deploy.readiness.http.client.HTTPConnection", return_value=connection):
            check(VALID)
        resolve.assert_called_once()
        create_connection.assert_called_once_with(("192.0.2.10", 443), timeout=5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="review.example")
        self.assertEqual(connection.sock, tls)

    def test_accepts_a_same_origin_prefixed_callback_and_matching_readiness_path(self):
        prefixed = {
            **VALID,
            "review_origin": "https://regkb.example",
            "oauth_callback": "https://regkb.example/review/auth/callback",
            "readiness_url": "https://regkb.example/review/__ready",
        }
        self.assertEqual(validate_configuration(prefixed), ("regkb.example", 443))
        with self.assertRaisesRegex(ReadinessError, "callback/origin mismatch"):
            validate_configuration({**prefixed, "readiness_url": "https://regkb.example/__ready"})

    def test_rejects_placeholder_values_recursively(self):
        for change in (
            {"reviewer_ids": ["[REVIEWER_IDS]"]},
            {"expected_addresses": ["CHANGEME"]},
            {"review_origin": "https://placeholder.invalid"},
            {"readiness_url": "https://review.example/[READINESS_PATH]"},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ReadinessError, "placeholder"):
                validate_configuration({**VALID, **change})

    def test_marker_binds_canonical_approved_config_without_values(self):
        payload = marker_payload(VALID, SECRET, confirmed_authorized_login=True, confirmed_unauthorized_login=True)
        self.assertEqual(set(payload), {"config_hmac", "issued_at", "expires_at", "schema", "status",
                                        "confirmed_authorized_login", "confirmed_unauthorized_login", "marker_hmac"})
        self.assertEqual(payload["status"], "verified")
        self.assertTrue(payload["confirmed_authorized_login"])
        self.assertTrue(payload["confirmed_unauthorized_login"])
        self.assertTrue(payload["issued_at"].endswith("Z"))
        self.assertNotEqual(payload["config_hmac"], hashlib.sha256(
            json.dumps(VALID, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
        rendered = json.dumps(payload, sort_keys=True)
        for secret_value in ("enterprise-123", "192.0.2.10", "review.example"):
            self.assertNotIn(secret_value, rendered)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "automation-ready"
            write_marker(target, VALID, SECRET, confirmed_authorized_login=True, confirmed_unauthorized_login=True,
                         clock=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            written=json.loads(target.read_text())
            self.assertEqual(written["confirmed_authorized_login"],payload["confirmed_authorized_login"])
            self.assertEqual(set(written),set(payload))
            self.assertEqual(target.stat().st_mode & 0o777, 0o444)
            self.assertRegex(written["marker_hmac"],r"^[0-9a-f]{64}$")


class StageARunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runbook = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        cls.matrix = Path("deploy/DAC_MATRIX.tsv").read_text(encoding="utf-8")

    def test_qa_template_stays_disabled_but_runbook_builds_and_validates_production_config(self):
        template = Path("qa/config/config.yaml").read_text(encoding="utf-8")
        self.assertIn("enabled: false", template)
        for required in (
            "qa_profile.py",
            "load_gateway_config",
            "DingTalkAdapter",
            "get_tool_definitions",
            "AUTHORIZED_QA_TESTER",
            "UNAUTHORIZED_QA_TESTER",
            "authorized message",
            "unauthorized message",
        ):
            self.assertIn(required, self.runbook)

    def test_review_route_configs_are_installed_with_atomic_same_directory_replacements(self):
        for command in (
            "mv -Tf /etc/nginx/snippets/.dek-review-location.conf.new /etc/nginx/snippets/dek-review-location.conf",
            "mv -Tf /etc/nginx/sites-available/.regkb.chenponai.com.new /etc/nginx/sites-available/regkb.chenponai.com",
            "mv -Tf /etc/nginx/sites-available/.dek-review.conf.new /etc/nginx/sites-available/dek-review.conf",
        ):
            self.assertIn(command, self.runbook)
        self.assertNotIn("[REVIEW_FULLCHAIN]", self.runbook)
        self.assertNotIn("[REVIEW_PRIVATE_KEY]", self.runbook)

    def test_review_service_restarts_before_nginx_publishes_the_new_route(self):
        restart_command = "systemctl restart dek-review.service"
        self.assertIn(restart_command, self.runbook)
        restart = self.runbook.index(restart_command)
        reload_nginx = self.runbook.index("systemctl reload nginx")
        first_live_nginx_write = self.runbook.index("/etc/nginx/snippets/.dek-review-location.conf.new")
        self.assertLess(restart, reload_nginx)
        self.assertLess(restart, first_live_nginx_write)
        self.assertNotIn("systemctl start dek-review.service", self.runbook)
        self.assertEqual(self.runbook.count("systemctl reload nginx"), 3)

    def test_nginx_worker_is_authorized_and_probes_the_review_socket_before_publish(self):
        membership = 'usermod -a -G dek-review-proxy "$NGINX_WORKER_USER"'
        probe = 'runuser -u "$NGINX_WORKER_USER" -- curl --fail --silent --show-error --unix-socket /run/dek-review/dek-review.sock http://localhost/__ready'
        first_live_nginx_write = self.runbook.index("/etc/nginx/snippets/.dek-review-location.conf.new")
        self.assertIn("NGINX_WORKER_USER='[NGINX_WORKER_USER]'", self.runbook)
        self.assertIn(membership, self.runbook)
        self.assertIn(probe, self.runbook)
        self.assertLess(self.runbook.index(membership), self.runbook.index("systemctl restart dek-review.service"))
        self.assertLess(self.runbook.index(probe), first_live_nginx_write)

    def test_review_route_publish_rolls_back_on_any_post_restart_failure(self):
        state_capture = self.runbook.index('REVIEW_WAS_ACTIVE="$(systemctl is-active dek-review.service || :)"')
        start = self.runbook.index("if ! (", state_capture)
        rollback = self.runbook.index("then\n  if ! python3 -I /run/dek-package-check/deploy/rollback.py", start)
        guarded = self.runbook[start:rollback]
        self.assertNotIn("set -e", guarded)
        self.assertIn("systemctl restart dek-review.service", guarded)
        for command in (
            "nginx -t || exit 1",
            "systemctl reload nginx || exit 1",
            "systemctl is-active --quiet nginx || exit 1",
            "python3 -I /run/dek-package-check/deploy/readiness.py --config /etc/dek-readiness.json || exit 1",
        ):
            self.assertIn(command, guarded)
        recovery = self.runbook[rollback:self.runbook.index("# Perform the approved real login tests", rollback)]
        self.assertIn("if ! python3 -I /run/dek-package-check/deploy/rollback.py", recovery)
        self.assertIn("Exact manual rollback required: review route rollback failed", recovery)
        self.assertIn("REVIEW_WAS_SUBSTATE", self.runbook[:start])
        self.assertIn("active:running|inactive:dead", self.runbook[:start])
        self.assertIn('test "$(systemctl show dek-review.service -p ActiveState --value)" = "$REVIEW_WAS_ACTIVE" || exit 1', recovery)
        self.assertIn('test "$(systemctl show dek-review.service -p SubState --value)" = "$REVIEW_WAS_SUBSTATE" || exit 1', recovery)
        self.assertIn("nginx -t", recovery)
        self.assertIn("systemctl reload nginx", recovery)
        self.assertIn("systemctl is-active --quiet nginx", recovery)

    def test_post_readiness_promotion_and_enablement_stop_on_every_failed_gate(self):
        start = self.runbook.index("# Perform the approved real login tests before continuing")
        end = self.runbook.index("```", start)
        final = self.runbook[start:end]
        for guarded in (
            "--write-marker /var/lib/dek-readiness/automation-ready --confirm-authorized-login --confirm-unauthorized-login || exit 1",
            "--validate-marker --marker /var/lib/dek-readiness/automation-ready || exit 1",
            'test ! -e "$FINAL_REPORT" -a ! -e "$FINAL_EXPECTED" || exit 1',
            "systemctl start dek-source-ingest.service || exit 1",
            "systemctl status dek-source-ingest.service --no-pager > \"$LATEST_BACKUP/ingestion-cutover/new.service.status\" || test \"$?\" -eq 3 || exit 1",
            "python3 - \"$FINAL_REPORT\" \"$FINAL_EXPECTED\" <<'PY' || exit 1",
            'sha256sum "$FINAL_REPORT" > "$LATEST_BACKUP/ingestion-cutover/new.report.sha256" || exit 1',
            "--journal-dir /var/lib/dek-install-transactions || exit 1",
            "systemctl enable --now dek-source-ingest.timer || exit 1",
            "systemctl is-active --quiet dek-source-ingest.timer || exit 1",
            "systemctl enable --now dek-review.service dek-review-publish-manual.path dek-source-ingest-manual.path || exit 1",
            "for unit in dek-review.service dek-review-publish-manual.path dek-source-ingest-manual.path; do",
            'systemctl is-active --quiet "$unit" || exit 1',
            'test "$(systemctl show "$unit" -p UnitFileState --value)" = enabled || exit 1',
        ):
            self.assertIn(guarded, final)
        self.assertNotIn("dek-builder.timer", final)
        self.assertNotIn("dek-review-publish.timer", final)
        self.assertNotIn("dek-activator.timer", final)

    def test_automation_marker_requires_confirmed_real_login_checks(self):
        self.assertIn("--confirm-authorized-login",self.runbook)
        self.assertIn("--confirm-unauthorized-login",self.runbook)
        self.assertIn("seven-day lease",self.runbook)
        self.assertIn("24 hours before",self.runbook)
        self.assertIn("every `ExecCondition`",self.runbook)
        self.assertNotIn("install -o root -g root -m 0644 /dev/null /var/lib/dek-readiness/automation-ready",self.runbook)

    def test_a2_binds_all_credentials_and_shared_consumers(self):
        for required in (
            "git-credential-to-publisher",
            "git-credential-to-source-ingest",
            "approval-signing-private",
            "review-decision-to-review",
            "review-decision-to-publisher",
        ):
            self.assertIn(required, self.matrix)
        self.assertIn("openssl pkey -in", self.runbook)
        self.assertIn("cmp -s", self.runbook)
        self.assertIn("dek-review/secrets/review-decision-key", self.runbook)
        self.assertIn("dek-publisher/secrets/review-decision-key", self.runbook)

    def test_rollback_anchor_and_ancestors_checked_before_unpack(self):
        check = self.runbook.index("root-owned regular file")
        unpack = self.runbook.index("rollback.py")
        self.assertLess(check, unpack)
        self.assertIn("non-root-writable ancestor", self.runbook)
        self.assertIn("immediately before archive extraction", self.runbook)

    def test_activator_network_is_loopback_only(self):
        unit = Path("deploy/systemd/dek-activator.service").read_text(encoding="utf-8")
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=localhost", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", unit)


if __name__ == "__main__":
    unittest.main()
