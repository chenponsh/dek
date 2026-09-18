from __future__ import annotations

import json
import os
import tempfile
import unittest
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml


class QaAliasClosureTests(unittest.TestCase):
    def test_every_hermes_platform_alias_is_disabled_and_nested_tools_are_removed(self):
        from deploy.qa_profile import migrate_profile

        source = {
            "group_sessions_per_user": True,
            "session_reset": {"mode": "both", "idle_minutes": 15},
            "platforms": {
                "dingtalk": {"enabled": False, "extra": {
                    "allowed_users": ["old"], "allowed_chats": ["chat"],
                    "require_mention": True, "client_secret": "keep-dingtalk-secret",
                }},
                "telegram": {"enabled": True, "token": "keep-telegram-secret"},
            },
            "telegram": {"enabled": True, "token": "keep-top-secret", "tools": ["terminal"]},
            "discord": {"enabled": True, "token": "keep-discord-secret"},
            "gateway": {
                "telegram": {"enabled": True, "tools": ["terminal"]},
                "platforms": {"discord": {"enabled": True, "toolsets": ["hermes-cli"]}},
                "platform_toolsets": {"discord": ["terminal"]},
            },
        }

        migrated = migrate_profile(source, "/var/lib/dek-qa/venvs/digest/bin/python")

        self.assertFalse(migrated["telegram"]["enabled"])
        self.assertFalse(migrated["discord"]["enabled"])
        self.assertFalse(migrated["gateway"]["telegram"]["enabled"])
        self.assertFalse(migrated["gateway"]["platforms"]["discord"]["enabled"])
        self.assertNotIn("tools", migrated["telegram"])
        self.assertNotIn("tools", migrated["gateway"]["telegram"])
        self.assertNotIn("toolsets", migrated["gateway"]["platforms"]["discord"])
        self.assertNotIn("platform_toolsets", migrated["gateway"])
        self.assertEqual("keep-top-secret", migrated["telegram"]["token"])
        self.assertEqual("keep-discord-secret", migrated["discord"]["token"])
        self.assertEqual("keep-dingtalk-secret", migrated["platforms"]["dingtalk"]["extra"]["client_secret"])


class IngestionInvocationBindingTests(unittest.TestCase):
    def test_report_requires_exact_nonce_and_strict_fresh_utc_generated_at(self):
        from deploy.source_ingest_entrypoint import persist_current_report, report_snapshot

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "_" / "ingestion"
            reports.mkdir(parents=True)
            output = root / "proof.json"
            started = datetime(2026, 9, 16, 1, 2, 3, tzinfo=timezone.utc)
            nonce = "invocation-nonce-with-high-entropy-1234567890"

            def attempt(generated_at: str, run_nonce: str) -> None:
                for path in reports.iterdir():
                    path.unlink()
                before = report_snapshot(root)
                report = reports / "scheduled-run-20260916_0102.json"
                report.write_text(json.dumps({
                    "mode": "scheduled-run", "date": "2026-09-16",
                    "generated_at": generated_at, "run_nonce": run_nonce,
                    "report": {}, "rough_created": [],
                }), encoding="utf-8")
                persist_current_report(root, before, output, run_nonce=nonce, started_at=started)

            with self.assertRaises(RuntimeError):
                attempt("2026-09-16T01:02:04Z", "wrong-nonce")
            with self.assertRaises(RuntimeError):
                attempt("2026-09-16T01:02:04+00:00", nonce)
            with self.assertRaises(RuntimeError):
                attempt("2026-09-16T01:02:02Z", nonce)
            with self.assertRaises(RuntimeError):
                attempt("2026-09-17T01:02:04Z", nonce)

            attempt("2026-09-16T01:02:04Z", nonce)
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            self.assertEqual(nonce, json.loads(output.read_text())["run_nonce"])

    def test_a6_has_distinct_private_staged_and_formal_expected_bindings(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]
        for token in (
            'STAGED_EXPECTED="$STAGE_ROOT/ingestion/proofs/staged-proof-report.expected.json"',
            "FINAL_EXPECTED=/var/lib/dek-source-ingest/proofs/latest-report.expected.json",
            "report.get('run_nonce')!=expected.get('run_nonce')",
            "started<=generated<=now+timedelta(minutes=5)",
            "generated-started>timedelta(hours=24)",
            ":600:1:regular file",
        ):
            self.assertIn(token, a6)


class ExactRollbackTreeTests(unittest.TestCase):
    def test_pre_restore_removes_every_inventory_root_including_new_descendants(self):
        from deploy.rollback import remove_inventory_roots

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "opt" / "dek-source-ingest"
            (root / "versions" / "old").mkdir(parents=True)
            (root / "versions" / "new-package").mkdir()
            (root / "clones" / "run-new").mkdir(parents=True)
            (root / "proofs").mkdir()
            (root / "proofs" / "new-report.json").write_text("new")

            remove_inventory_roots([str(root)], require_root_owned_ancestors=False)

            self.assertFalse(root.exists())

    def test_rollback_removal_is_safely_retryable_after_a_partial_crash(self):
        """rollback.py has no journal/recover step (unlike a6_cutover.py); its
        crash-recovery story is instead: an operator just re-runs main() from
        scratch. That is only safe if removing inventory roots a second time,
        after some are already gone (simulating a kill between roots on the
        first attempt), is a no-op rather than an error."""
        from deploy.rollback import remove_inventory_roots

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            first = parent / "opt" / "dek-source-ingest"
            second = parent / "opt" / "dek-review"
            first.mkdir(parents=True)
            second.mkdir(parents=True)

            # Simulate a first attempt that crashed after removing only `first`.
            remove_inventory_roots([str(first)], require_root_owned_ancestors=False)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

            # A naive retry-from-scratch must tolerate the already-removed root.
            remove_inventory_roots([str(first), str(second)], require_root_owned_ancestors=False)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_backup_tree_manifest_detects_any_post_restore_extra(self):
        from deploy.rollback import archive_tree_manifest, filesystem_tree_manifest

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"; restored = base / "restored"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "old.txt").write_text("old", encoding="utf-8")
            archive = base / "files.tar"
            with tarfile.open(archive, "w") as handle:
                handle.add(source, arcname=restored.as_posix().lstrip("/"))
            expected = archive_tree_manifest(archive)
            with tarfile.open(archive, "r:") as handle:
                handle.extractall("/", filter="data")
            self.assertEqual(expected, filesystem_tree_manifest([str(restored)]))
            (restored / "new-package").mkdir()
            self.assertNotEqual(expected, filesystem_tree_manifest([str(restored)]))


class CredentialContentGateTests(unittest.TestCase):
    def test_qa_environment_rejects_provider_key_that_real_hermes_expands_to_x_search(self):
        from deploy.credential_gate import CredentialError, _environment
        from deploy.qa_profile import migrate_profile
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            profile.mkdir()
            migrated = migrate_profile({
                "group_sessions_per_user": True,
                "session_reset": {"mode": "both", "idle_minutes": 15},
                "platforms": {"dingtalk": {"enabled": True, "extra": {
                    "allowed_users": ["*"], "allowed_chats": ["qa-chat"],
                    "require_mention": True,
                }}},
            }, "/usr/bin/python3")
            (profile / "config.yaml").write_text(
                yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            environment = root / "environment"
            environment.write_text(
                "DINGTALK_CLIENT_ID=ding-client-918273\n"
                "DINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\n"
                "DINGTALK_AGENT_ID=123456\n"
                "DINGTALK_ALLOWED_USERS=*\n"
                "DINGTALK_ALLOW_ALL_USERS=true\n"
                "XAI_API_KEY=xai-7M4q9Vz2Lc8Np6Rx3Wd5Hs8Y\n",
                encoding="utf-8",
            )
            hermes_environment = {
                "DINGTALK_CLIENT_ID": "ding-client-918273",
                "DINGTALK_CLIENT_SECRET": "Q7vN2xL9pR4mT8kW6sH3",
                "DINGTALK_AGENT_ID": "123456",
                "DINGTALK_ALLOWED_USERS": "*",
                "DINGTALK_ALLOW_ALL_USERS": "true",
                "XAI_API_KEY": "xai-7M4q9Vz2Lc8Np6Rx3Wd5Hs8Y",
            }
            with patch.dict(os.environ, {
                **hermes_environment,
                "HOME": str(root),
                "HERMES_HOME": str(profile),
            }, clear=True):
                parsed = load_config()
                enabled = sorted(_get_platform_tools(parsed, "dingtalk"))
                final = {
                    item.get("function", {}).get("name")
                    for item in get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
                }
            self.assertIn("x_search", enabled)
            self.assertIn("x_search", final)
            with self.assertRaises(CredentialError):
                _environment(environment, "qa")

    def test_qa_environment_fail_closes_known_hermes_control_fields_without_values(self):
        from deploy.credential_gate import CredentialError, _environment

        controls = (
            "OPENAI_API_KEY", "HASS_TOKEN", "TELEGRAM_BOT_TOKEN",
            "GATEWAY_MULTIPLEX_PROFILES", "HERMES_OPTIONAL_MCPS",
            "HERMES_PROFILE", "TERMINAL_ENV",
        )
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "environment"
            baseline = (
                "DINGTALK_CLIENT_ID=ding-client-918273\n"
                "DINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\n"
                "DINGTALK_AGENT_ID=123456\n"
                "DINGTALK_ALLOWED_USERS=*\n"
                "DINGTALK_ALLOW_ALL_USERS=true\n"
            )
            for name in controls:
                with self.subTest(name=name):
                    value = "control-value-7M4q9Vz2Lc8Np6Rx3Wd5Hs8Y"
                    environment.write_text(baseline + f"{name}={value}\n", encoding="utf-8")
                    with self.assertRaises(CredentialError) as raised:
                        _environment(environment, "qa")
                    self.assertNotIn(value, str(raised.exception))


    def test_rejects_placeholders_short_low_entropy_and_mismatched_keys_without_echo(self):
        from deploy.credential_gate import CredentialError, validate_secret_bytes

        bad = (b"", b"***", b"CHANGEME", b"example-secret", b"a" * 64, b"1234567890")
        for value in bad:
            with self.subTest(length=len(value)):
                with self.assertRaises(CredentialError) as raised:
                    validate_secret_bytes(value, label="review decision key", minimum=32)
                if value:
                    self.assertNotIn(value.decode("utf-8", "ignore"), str(raised.exception))

    def test_gate_validates_git_url_environment_fields_and_ed25519_pair(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from deploy.credential_gate import CredentialError, validate_credential_set

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            other = Ed25519PrivateKey.generate()
            private_path = root / "private.pem"
            public_path = root / "public.pem"
            git_path = root / "git"
            environment = root / "environment"
            private_path.write_bytes(private.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            public_path.write_bytes(other.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            git_path.write_text("https://user:high-entropy-token-1234567890@github.com/chenponsh/dek.git\n")
            environment.write_text("DINGTALK_CLIENT_ID=***\nDINGTALK_CLIENT_SECRET=example\n")
            for path in (private_path, public_path, git_path, environment):
                os.chmod(path, 0o400)

            with self.assertRaises(CredentialError):
                validate_credential_set(
                    git_credential=git_path, fixed_origin="https://github.com/chenponsh/dek.git",
                    approval_private=private_path, approval_public=public_path,
                    environments={"qa": environment}, shared_secret_pairs=[], random_secrets=[],
                )

    def test_complete_matching_credential_set_passes_without_exposing_values(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from deploy.credential_gate import validate_credential_set

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); key = Ed25519PrivateKey.generate()
            private = root / "private"; public = root / "public"; git = root / "git"
            qa = root / "qa"; left = root / "left"; right = root / "right"; random = root / "random"
            private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            git.write_text("https://robot:token-7M4q9Vz2Lc8Np6Rx@github.com/chenponsh/dek.git\n")
            qa.write_text("DINGTALK_CLIENT_ID=ding-client-918273\nDINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\nDINGTALK_AGENT_ID=123456\nDINGTALK_ALLOWED_USERS=*\nDINGTALK_ALLOW_ALL_USERS=true\n")
            secret = bytes(range(33, 65)); left.write_bytes(secret); right.write_bytes(secret); random.write_bytes(bytes(range(96, 64, -1)))
            validate_credential_set(
                git_credential=git, fixed_origin="https://github.com/chenponsh/dek.git",
                approval_private=private, approval_public=public, environments={"qa": qa},
                shared_secret_pairs=[(left, right)], random_secrets=[("random", random)],
            )

    def test_qa_environment_accepts_lowercase_proxy_names_and_hermes_chat_fields(self):
        """The real deployed dek-qa environment file sets both HTTP_PROXY and
        http_proxy (different HTTP client libraries check different casing),
        plus DINGTALK_ALLOWED_CHATS/DINGTALK_REQUIRE_MENTION, and omits
        DINGTALK_AGENT_ID entirely (Hermes's DingTalk bot adapter never reads
        it -- only web/app.py's separate OAuth login flow does) -- all
        discovered running credential_gate.py against the live file for the
        first time."""
        from deploy.credential_gate import _environment

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment"
            path.write_text(
                "DINGTALK_CLIENT_ID=ding-client-918273\n"
                "DINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\n"
                "DINGTALK_ALLOWED_USERS=*\n"
                "DINGTALK_ALLOW_ALL_USERS=true\n"
                "DINGTALK_ALLOWED_CHATS=chat-a,chat-b\n"
                "DINGTALK_REQUIRE_MENTION=true\n"
                "HTTP_PROXY=http://127.0.0.1:7890\n"
                "http_proxy=http://127.0.0.1:7890\n"
                "HTTPS_PROXY=http://127.0.0.1:7890\n"
                "https_proxy=http://127.0.0.1:7890\n"
                "NO_PROXY=example.internal\n"
                "no_proxy=example.internal\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o400)
            values = _environment(path, "qa")
            self.assertEqual(values["http_proxy"], b"http://127.0.0.1:7890")
            self.assertEqual(values["DINGTALK_ALLOWED_CHATS"], b"chat-a,chat-b")
            self.assertEqual(values["DINGTALK_REQUIRE_MENTION"], b"true")

    def test_tls_pair_validation_follows_the_standard_letsencrypt_live_symlink(self):
        """certbot's real /etc/letsencrypt/live/<domain>/{fullchain,privkey}.pem
        are always symlinks into ../../archive/<domain>/. TLS validation had
        zero test coverage before this and, when actually run against the
        live cert on this host for the first time, failed outright: the
        shared read helper opens with O_NOFOLLOW and rejects a symlink path
        rather than resolving the expected one hop into archive/."""
        import datetime as dt
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from deploy.credential_gate import CredentialError, validate_credential_set

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive" / "example.test"; archive.mkdir(parents=True)
            live = root / "live" / "example.test"; live.mkdir(parents=True)

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.test")])
            now = dt.datetime.now(dt.timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=90))
                .sign(key, hashes.SHA256())
            )
            (archive / "privkey1.pem").write_bytes(
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (archive / "fullchain1.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            (live / "privkey.pem").symlink_to("../../archive/example.test/privkey1.pem")
            (live / "fullchain.pem").symlink_to("../../archive/example.test/fullchain1.pem")

            approval_key = Ed25519PrivateKey.generate()
            approval_private = root / "approval-private"; approval_public = root / "approval-public"
            approval_private.write_bytes(approval_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            approval_public.write_bytes(approval_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            git_credential = root / "git-credential"
            git_credential.write_text("https://robot:token-7M4q9Vz2Lc8Np6Rx@github.com/chenponsh/dek.git\n")

            validate_credential_set(
                git_credential=git_credential, fixed_origin="https://github.com/chenponsh/dek.git",
                approval_private=approval_private, approval_public=approval_public,
                environments={}, shared_secret_pairs=[], random_secrets=[],
                tls_pair=(live / "fullchain.pem", live / "privkey.pem"),
            )

            mismatched_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            (archive / "privkey1.pem").write_bytes(
                mismatched_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with self.assertRaisesRegex(CredentialError, "do not match"):
                validate_credential_set(
                    git_credential=git_credential, fixed_origin="https://github.com/chenponsh/dek.git",
                    approval_private=approval_private, approval_public=approval_public,
                    environments={}, shared_secret_pairs=[], random_secrets=[],
                    tls_pair=(live / "fullchain.pem", live / "privkey.pem"),
                )

    def test_qa_environment_still_rejects_an_unapproved_lowercase_name(self):
        from deploy.credential_gate import CredentialError, _environment

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment"
            path.write_text(
                "DINGTALK_CLIENT_ID=ding-client-918273\n"
                "DINGTALK_CLIENT_SECRET=Q7vN2xL9pR4mT8kW6sH3\n"
                "DINGTALK_AGENT_ID=123456\n"
                "DINGTALK_ALLOWED_USERS=*\n"
                "DINGTALK_ALLOW_ALL_USERS=true\n"
                "some_other_lowercase_name=x\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o400)
            with self.assertRaisesRegex(CredentialError, "invalid or duplicated"):
                _environment(path, "qa")


class QaA6EnvironmentAcceptanceContractTests(unittest.TestCase):
    def test_qa_profile_acceptance_surface_is_exactly_three_read_only_tools(self):
        from deploy.qa_profile import QA_READ_ONLY_TOOLS

        self.assertEqual(QA_READ_ONLY_TOOLS, frozenset({
            "mcp__dek_kb__dek_kb_search",
            "mcp__dek_kb__dek_kb_get",
            "mcp__dek_kb__dek_kb_recent",
        }))

    def test_qa_operator_docs_list_the_exact_three_tool_acceptance_surface(self):
        expected = {
            "mcp__dek_kb__dek_kb_search",
            "mcp__dek_kb__dek_kb_get",
            "mcp__dek_kb__dek_kb_recent",
        }
        for path in (Path("qa/README.md"), Path("qa/HANDOVER.md")):
            text = path.read_text(encoding="utf-8")
            observed = {
                token for token in expected
                if token in text
            }
            self.assertEqual(expected, observed, path.as_posix())

    def test_qa_operator_docs_do_not_describe_the_retired_two_tool_surface(self):
        stale_descriptions = {
            Path("qa/README.md"): (
                "MCP 仅公开 `dek_kb_search` 与 `dek_kb_get` 两个只读工具",
            ),
            Path("qa/HANDOVER.md"): (
                "只暴露 `dek_kb_search`、`dek_kb_get`。",
                "仅提供 `dek_kb_search` 和 `dek_kb_get`",
                "MCP discovery：2 个工具",
                "模型工具定义：2 个，且仅为上述 search/get",
                "均发现且只发现 `dek_kb_search`、`dek_kb_get`。",
                "最终工具只能是两个 dek MCP 工具",
                "DEK MCP 仍只有 search/get。",
            ),
            Path("qa/DEPLOYMENT_PLAN.md"): (
                "最终仅有两个 dek MCP 工具",
                "最终只有 `dek_kb_search`、`dek_kb_get`；",
            ),
            Path("deploy/PRODUCTION_ROLLOUT.md"): (
                "exactly two model-visible tools",
                "only `dek_kb_search` and `dek_kb_get`",
            ),
        }
        for path, retired_phrases in stale_descriptions.items():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.as_posix()):
                self.assertEqual(
                    [],
                    [phrase for phrase in retired_phrases if phrase in text],
                )

        stale_test_name_fragment = "exactly" + "_two_tools"
        stale_test_references = [
            path.as_posix()
            for path in sorted(Path("tests").rglob("test_*.py"))
            if stale_test_name_fragment in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], stale_test_references)

    def test_a6_rechecks_then_systemd_loads_formal_environment_for_real_hermes_assembly(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]
        gate = a6.index("credential_gate.py")
        systemd = a6.index("systemd-run")
        restart = a6.index("a6_cutover.py apply")
        self.assertLess(gate, systemd)
        self.assertLess(systemd, restart)
        for required in (
            "EnvironmentFile=/var/lib/dek-qa/secrets/environment",
            "--environment-file /var/lib/dek-qa/secrets/environment",
            '--validate-existing "$QA_PROFILE/config.yaml"',
            '--runtime-python-executable "$QA_CANDIDATE/bin/python"',
            "load_gateway_config()",
            "DingTalkAdapter",
            "discover_mcp_tools()",
            "get_tool_definitions()",
            "mcp__dek_kb__dek_kb_search",
            "mcp__dek_kb__dek_kb_get",
            "mcp__dek_kb__dek_kb_recent",
            "only enabled adapter is `dingtalk`",
        ):
            self.assertIn(required, a6)


if __name__ == "__main__":
    unittest.main()
