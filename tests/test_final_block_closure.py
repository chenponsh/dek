from __future__ import annotations

import json
import os
import tempfile
import unittest
import hashlib
import io
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.activator import Activator, ActivatorConfig
from web.app import ActiveSite
from deploy.install_components import SERVICES, install_versioned_components, reconcile_install_transactions
from deploy.readiness import ReadinessError, write_marker, _config_hmac
from deploy.rollback import REQUIRED, validate_archive, validate_program, verify_anchor


def _hermes_gateway_available() -> bool:
    """importlib.util.find_spec("gateway.config") requires the parent package
    "gateway" to already be importable to search within it -- when Hermes
    isn't installed at all, it raises ModuleNotFoundError instead of
    returning None, which crashed test collection for this whole module."""
    import importlib.util
    try:
        return importlib.util.find_spec("gateway.config") is not None
    except ModuleNotFoundError:
        return False


class ReleaseLockLifetimeTests(unittest.TestCase):
    def test_web_locks_generation_before_reading_release_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            release = releases / "generation-one"
            release.mkdir(parents=True)
            (release / "site").mkdir()
            (release / "release.lock").touch()
            metadata = {"generation": "generation-one"}
            (release / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
            active = root / "active.json"
            active.write_text(json.dumps(metadata), encoding="utf-8")
            events = []
            real_read_text = Path.read_text

            def observed_read(path, *args, **kwargs):
                if path == release / "release.json":
                    events.append("release-read")
                return real_read_text(path, *args, **kwargs)

            with patch("web.app.fcntl.flock", side_effect=lambda *_: events.append("lock")), \
                    patch("pathlib.Path.read_text", new=observed_read):
                pin = ActiveSite(active, releases).pin()
                pin.close()
            self.assertEqual(["lock", "release-read"], events)

    def test_cleanup_holds_exclusive_lock_through_rmtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = ActivatorConfig.under(root)
            config = ActivatorConfig(base.build_inbox, base.releases, base.control, base.journal,
                                     base.outcomes, base.spent, base.active, retain=0)
            config.prepare()
            stale = config.releases / "stale"
            stale.mkdir()
            (stale / "release.lock").touch()
            closed = False
            real_close = os.close

            def observed_close(descriptor):
                nonlocal closed
                closed = True
                return real_close(descriptor)

            def observed_rmtree(path):
                self.assertFalse(closed, "release lock closed before destructive cleanup")

            activator = Activator(config, proof_reader=lambda *_: {})
            with patch("deploy.activator.os.close", side_effect=observed_close), \
                    patch("deploy.activator.shutil.rmtree", side_effect=observed_rmtree):
                activator.cleanup()


class ComponentTransactionTests(unittest.TestCase):
    def fixture(self, root: Path):
        package = root / "package"
        package.mkdir()
        lines = []
        for prefixes in SERVICES.values():
            for prefix in prefixes:
                relative = prefix + "payload.txt"
                path = package / relative
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(relative, encoding="utf-8")
                    lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
        manifest = package / "PACKAGE.sha256"
        manifest.write_text("".join(lines), encoding="utf-8")
        roots = {}
        originals = {}
        for service in SERVICES:
            service_root = root / service
            old = service_root / "versions" / "old"
            old.mkdir(parents=True)
            (service_root / "current").symlink_to("versions/old")
            (service_root / "app").symlink_to("current")
            (service_root / "previous").symlink_to("versions/older")
            (service_root / "install-manifest.expected").write_text("old expected\n")
            (service_root / "install-manifest.actual").write_text("old actual\n")
            roots[service] = service_root
            originals[service] = {
                "links": tuple(os.readlink(service_root / name) for name in ("current", "app", "previous")),
                "manifests": tuple((service_root / name).read_bytes() for name in ("install-manifest.expected", "install-manifest.actual")),
            }
        journal = root / "journal"
        journal.mkdir(mode=0o700)
        return package, manifest, roots, originals, journal

    def test_reconcile_after_crash_restores_links_and_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, manifest, roots, originals, journal = self.fixture(Path(temporary))

            class Crash(BaseException):
                pass

            with self.assertRaises(Crash):
                install_versioned_components(
                    package, manifest, roots, "a" * 64, journal_dir=journal,
                    after_step=lambda step: (_ for _ in ()).throw(Crash()) if step == "dek-web:current" else None,
                    test_only_allow_unsafe_ancestors={package.parent.parent},
                )
            self.assertTrue(any(journal.iterdir()), "crash must leave a persistent transaction journal")
            reconcile_install_transactions(
                journal, test_only_allow_unsafe_ancestors={package.parent.parent},
            )
            self.assertEqual([], list(journal.iterdir()))
            for service, service_root in roots.items():
                self.assertEqual(originals[service]["links"], tuple(
                    os.readlink(service_root / name) for name in ("current", "app", "previous")
                ))
                self.assertEqual(originals[service]["manifests"], tuple(
                    (service_root / name).read_bytes() for name in ("install-manifest.expected", "install-manifest.actual")
                ))

    def test_a3_can_prepare_source_ingest_without_switching_its_live_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, manifest, roots, originals, journal = self.fixture(Path(temporary))
            install_versioned_components(
                package, manifest, roots, "b" * 64, journal_dir=journal,
                deferred_services={"dek-source-ingest"},
                test_only_allow_unsafe_ancestors={package.parent.parent},
            )
            source = roots["dek-source-ingest"]
            self.assertEqual(originals["dek-source-ingest"]["links"], tuple(
                os.readlink(source / name) for name in ("current", "app", "previous")
            ))
            self.assertTrue((source / "versions" / ("b" * 64)).is_dir())


class SourceIngestProofTests(unittest.TestCase):
    def test_entrypoint_persists_this_run_report_before_return_or_clone_removal(self):
        source = Path("deploy/source_ingest_entrypoint.py").read_text(encoding="utf-8")
        dispatch = source.index('candidate_argv.append("--no-publication")')
        run = source.index("result=module.main(candidate_argv)")
        persisted = source.index("persist_current_report(repo, before_reports, args.proof_output,")
        early_return = source.index("if result: return result")
        clone_removal = source.index("shutil.rmtree(temporary)")
        self.assertLess(dispatch, run)
        self.assertLess(run, persisted)
        self.assertLess(persisted, early_return)
        self.assertLess(persisted, clone_removal)

    def test_current_run_report_is_atomically_persisted_with_private_mode(self):
        from deploy.source_ingest_entrypoint import report_snapshot, persist_current_report

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "_" / "ingestion"
            reports.mkdir(parents=True)
            old = reports / "scheduled-run-20260916_0900.json"
            old.write_text('{"mode":"scheduled-run","date":"2026-09-16","generated_at":"old","report":{},"rough_created":[]}', encoding="utf-8")
            before = report_snapshot(root)
            current = reports / "scheduled-run-20260916_0915.json"
            nonce = "current-run-nonce-1234567890-abcdef"
            started = datetime(2026, 9, 16, 9, 14, tzinfo=timezone.utc)
            payload = {"mode": "scheduled-run", "date": "2026-09-16", "generated_at": "2026-09-16T09:15:00Z", "run_nonce": nonce, "report": {}, "rough_created": []}
            current.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "persistent" / "proof.json"
            output.parent.mkdir()

            selected = persist_current_report(root, before, output, run_nonce=nonce, started_at=started,
                                              now=datetime(2026, 9, 16, 9, 16, tzinfo=timezone.utc))

            self.assertEqual(selected, current)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(output.parent.glob(".report-*")), [])

    def test_report_persistence_rejects_missing_or_ambiguous_current_run(self):
        from deploy.source_ingest_entrypoint import report_snapshot, persist_current_report

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "_" / "ingestion"
            reports.mkdir(parents=True)
            before = report_snapshot(root)
            output = root / "proof.json"
            nonce = "current-run-nonce-1234567890-abcdef"
            started = datetime(2026, 9, 16, 9, 14, tzinfo=timezone.utc)
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                persist_current_report(root, before, output, run_nonce=nonce, started_at=started)
            for minute in ("0915", "0916"):
                (reports / f"scheduled-run-20260916_{minute}.json").write_text(
                    json.dumps({"mode": "scheduled-run", "date": "2026-09-16", "generated_at": "2026-09-16T09:15:00Z", "run_nonce": nonce, "report": {}, "rough_created": []}),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                persist_current_report(root, before, output, run_nonce=nonce, started_at=started)


class MarkerConfirmationRequirementTests(unittest.TestCase):
    """readiness.py was simplified from a three-file review/evidence/automation
    signing chain down to one marker, written only once an operator asserts
    (via --confirm-authorized-login/--confirm-unauthorized-login) that they
    just personally exercised both login checks."""
    CONFIG = {
        "review_origin": "https://review.example",
        "oauth_callback": "https://review.example/auth/callback",
        "reviewer_ids": ["enterprise-approved"],
        "expected_addresses": ["192.0.2.10"],
        "readiness_url": "https://review.example/__ready",
    }
    SECRET = b"structured-evidence-test-secret-32bytes"

    def test_write_marker_refuses_without_both_confirmations(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "automation-ready"
            with self.assertRaisesRegex(ReadinessError, "confirmed"):
                write_marker(target, self.CONFIG, self.SECRET,
                            confirmed_authorized_login=True, confirmed_unauthorized_login=False)
            self.assertFalse(target.exists())

    def test_write_marker_succeeds_with_both_confirmations(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "automation-ready"
            write_marker(target, self.CONFIG, self.SECRET,
                         confirmed_authorized_login=True, confirmed_unauthorized_login=True)
            payload = json.loads(target.read_text())
            self.assertTrue(payload["confirmed_authorized_login"])
            self.assertTrue(payload["confirmed_unauthorized_login"])

    def test_cli_write_marker_refuses_without_both_confirm_flags(self):
        from deploy.readiness import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "readiness.json"; key = root / "marker-hmac-key"; target = root / "automation-ready"
            config.write_text(json.dumps(self.CONFIG), encoding="utf-8")
            key.write_bytes(self.SECRET); os.chmod(key, 0o400)
            with patch("deploy.readiness.check"):
                with self.assertRaises(SystemExit):
                    main(["--config", str(config), "--hmac-key", str(key),
                         "--write-marker", str(target), "--confirm-authorized-login"])
                self.assertFalse(target.exists())
                main(["--config", str(config), "--hmac-key", str(key), "--write-marker", str(target),
                     "--confirm-authorized-login", "--confirm-unauthorized-login"])
                self.assertTrue(target.exists())


class RollbackSymlinkTests(unittest.TestCase):
    def test_rollback_program_requires_root_owned_0644_single_link_inode_and_trusted_ancestors(self):
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root=Path(temporary); os.chmod(root,0o700)
            program=root/"rollback.py"; program.write_text("pass\n"); os.chmod(program,0o644)
            validate_program(program)
            os.chmod(program,0o664)
            with self.assertRaisesRegex(SystemExit,"program"): validate_program(program)
            os.chmod(program,0o644); link=root/"copy.py"; os.link(program,link)
            with self.assertRaisesRegex(SystemExit,"program"): validate_program(program)

    def test_anchor_verification_rejects_world_writable_tmp_ancestor(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            base = Path(temporary); backup = base / "backup"; backup.mkdir(mode=0o700)
            lines = []
            for name in sorted(REQUIRED):
                item = backup / name
                item.write_bytes(name.encode("ascii"))
                os.chmod(item, 0o400)
                lines.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {name}\n")
            anchor = base / "dek-backup-SHA256SUMS"
            anchor.write_text("".join(lines), encoding="utf-8")
            os.chmod(anchor, 0o400)
            with self.assertRaisesRegex(SystemExit, "ancestor"):
                verify_anchor(backup, anchor)

    def test_archive_accepts_contained_symlink_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.tar"
            with tarfile.open(safe, "w") as archive:
                directory = tarfile.TarInfo("opt/dek-web"); directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                link = tarfile.TarInfo("opt/dek-web/current"); link.type = tarfile.SYMTYPE; link.linkname = "versions/old"
                archive.addfile(link)
            validate_archive(safe, ["/opt/dek-web"])

            escape = root / "escape.tar"
            with tarfile.open(escape, "w") as archive:
                directory = tarfile.TarInfo("opt/dek-web"); directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                link = tarfile.TarInfo("opt/dek-web/current"); link.type = tarfile.SYMTYPE; link.linkname = "../../etc/shadow"
                archive.addfile(link)
            with self.assertRaises(SystemExit):
                validate_archive(escape, ["/opt/dek-web"])

    def test_validate_archive_accepts_a_contained_hardlink_and_rejects_escape_or_forward_reference(self):
        """GNU tar always archives a shared inode's first occurrence as a
        regular file and every later occurrence as a hardlink (type '1')
        back to it -- real backups of Python venvs / node_modules are full
        of these. validate_archive() must accept a legitimate one, but still
        reject a hardlink that escapes the approved tree or references a
        member that was never actually archived."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.tar"
            with tarfile.open(safe, "w") as archive:
                directory = tarfile.TarInfo("opt/dek-web"); directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                original = tarfile.TarInfo("opt/dek-web/first"); original.type = tarfile.REGTYPE; original.size = 0
                archive.addfile(original)
                link = tarfile.TarInfo("opt/dek-web/second"); link.type = tarfile.LNKTYPE; link.linkname = "opt/dek-web/first"
                archive.addfile(link)
            validate_archive(safe, ["/opt/dek-web"])

            escape = root / "escape.tar"
            with tarfile.open(escape, "w") as archive:
                directory = tarfile.TarInfo("opt/dek-web"); directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                link = tarfile.TarInfo("opt/dek-web/second"); link.type = tarfile.LNKTYPE; link.linkname = "../../etc/shadow"
                archive.addfile(link)
            with self.assertRaisesRegex(SystemExit, "escapes"):
                validate_archive(escape, ["/opt/dek-web"])

            forward = root / "forward.tar"
            with tarfile.open(forward, "w") as archive:
                directory = tarfile.TarInfo("opt/dek-web"); directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                link = tarfile.TarInfo("opt/dek-web/second"); link.type = tarfile.LNKTYPE; link.linkname = "opt/dek-web/never-archived"
                archive.addfile(link)
            with self.assertRaisesRegex(SystemExit, "not an already-archived member"):
                validate_archive(forward, ["/opt/dek-web"])

    def test_archive_and_filesystem_tree_manifest_agree_on_a_restored_hardlink(self):
        from deploy.rollback import archive_tree_manifest, filesystem_tree_manifest

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"; restored = base / "restored"
            source.mkdir()
            (source / "first").write_bytes(b"shared content")
            archive = base / "files.tar"
            with tarfile.open(archive, "w") as handle:
                handle.add(source, arcname=restored.as_posix().lstrip("/"))
                info = tarfile.TarInfo((restored / "second").as_posix().lstrip("/"))
                info.type = tarfile.LNKTYPE
                info.linkname = (restored / "first").as_posix().lstrip("/")
                handle.addfile(info)
            expected = archive_tree_manifest(archive)
            file_entries = {entry["path"]: entry for entry in expected if entry["type"] == "file"}
            self.assertEqual(
                file_entries[restored.as_posix().lstrip("/") + "/first"]["sha256"],
                file_entries[restored.as_posix().lstrip("/") + "/second"]["sha256"],
                "the hardlink member must resolve to its target's real content digest",
            )
            with tarfile.open(archive, "r:") as handle:
                handle.extractall("/", filter="data")
            self.assertEqual(expected, filesystem_tree_manifest([str(restored)]))
            self.assertEqual(
                os.stat(restored / "first").st_ino, os.stat(restored / "second").st_ino,
                "tar -x must have recreated an actual hardlink, not a second independent file",
            )

    def test_runbook_backup_covers_web_and_qa_state_and_keeps_a6_evidence_separate(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("/var/lib/dek-web", text)
        self.assertIn("/var/lib/dek-qa", text)
        self.assertIn("A6 evidence is not part of the fixed backup metadata set", text)
        self.assertIn("--write-marker /var/lib/dek-readiness/automation-ready --confirm-authorized-login --confirm-unauthorized-login",text)
        self.assertIn("openat",text)
        self.assertIn("mode `0644`, single-link",text)


class QaStagingContractTests(unittest.TestCase):
    def test_runbook_uses_fresh_python312_runtime_and_defers_atomic_switch(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a5 = text[text.index("Checkpoint A5b"):text.index("Checkpoint A6")]
        a6 = text[text.index("Checkpoint A6"):text.index("Exact manual rollback")]
        for required in (
            "STAGE_ROOT=/var/lib/dek-stage-a/$PACKAGE_SHA256",
            'QA_CANDIDATE="$STAGE_ROOT/qa/venv"',
            '/usr/bin/python3.12 -m venv "$QA_CANDIDATE"',
            "--no-index --find-links '[QA_WHEELHOUSE]' --require-hashes",
            "pip install --no-deps '[QA_HERMES_WHEEL]'",
            "pip check",
            "qa_profile.py",
            "/var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml",
            "allowed_chats",
            "require_mention",
            "load_gateway_config",
            "DingTalkAdapter",
        ):
            self.assertIn(required, a5 + a6)
        self.assertNotIn("qa/config/config.yaml /var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml", a5)
        self.assertNotIn("-m venv /var/lib/dek-qa/venvs/$PACKAGE_SHA256", a5)
        self.assertIn("a6_cutover.py apply", a6)
        self.assertNotIn("/var/lib/dek-qa/.venv.new", a6)
        self.assertNotIn("/var/lib/dek-qa/hermes/profiles/.profile.new", a6)

    def test_profile_migration_preserves_chat_session_and_unknown_non_tool_constraints(self):
        from deploy.qa_profile import migrate_profile
        old = {
            "group_sessions_per_user": True,
            "session_reset": {"mode": "both", "idle_minutes": 15},
            "platform_toolsets": {"dingtalk": []},
            "tools": {"tool_search": {"enabled": "off"}},
            "platforms": {"dingtalk": {"enabled": False, "extra": {
                "allowed_users": ["old-user"], "allowed_chats": ["chat-a"], "require_mention": True,
                "preserved": "yes",
            }}},
            "mcp_servers": {"dek_kb": {
                "command": "/old/python",
                "args": ["-m", "qa.dek_qa.mcp_server", "--index", "/var/lib/dek-qa/index/dek-kb.json"],
                "env": {"PYTHONPATH": "/opt/dek-qa/app"},
                "sampling": {"enabled": False},
            }},
            "unrelated": {"keep": 7},
        }
        migrated = migrate_profile(old, "/var/lib/dek-qa/venvs/digest/bin/python")
        self.assertEqual(["chat-a"], migrated["platforms"]["dingtalk"]["extra"]["allowed_chats"])
        self.assertTrue(migrated["platforms"]["dingtalk"]["extra"]["require_mention"])
        self.assertEqual(old["session_reset"], migrated["session_reset"])
        self.assertEqual({"dingtalk": []}, migrated["platform_toolsets"])
        self.assertEqual({"tool_search": {"enabled": "off"}}, migrated["tools"])
        self.assertEqual({"keep": 7}, migrated["unrelated"])
        self.assertEqual("/var/lib/dek-qa/venvs/digest/bin/python", migrated["mcp_servers"]["dek_kb"]["command"])
        self.assertEqual([
            "-m", "qa.dek_qa.mcp_server",
            "--active", "/var/lib/dek-activate/control/active.json",
            "--releases", "/var/lib/dek-activate/releases",
            "--generation-proof", "/run/dek-proofs/qa/active.json",
        ], migrated["mcp_servers"]["dek_kb"]["args"])
        self.assertEqual({"PYTHONPATH": "/opt/dek-qa/app"}, migrated["mcp_servers"]["dek_kb"]["env"])
        self.assertEqual({"enabled": False}, migrated["mcp_servers"]["dek_kb"]["sampling"])

    def test_profile_migration_removes_every_unapproved_tool_surface(self):
        from deploy.qa_profile import migrate_profile
        old = {
            "group_sessions_per_user": True,
            "session_reset": {"mode": "both", "idle_minutes": 15},
            "platform_toolsets": {
                "dingtalk": ["terminal", "evil_mcp"],
                "telegram": ["terminal"],
            },
            "tools": {
                "tool_search": {"enabled": "on", "bridge": "unsafe"},
                "terminal": {"enabled": True},
            },
            "platforms": {
                "dingtalk": {"enabled": False, "extra": {
                    "allowed_users": ["old-user"],
                    "allowed_chats": ["chat-a"],
                    "require_mention": True,
                    "client_secret": "preserve-me",
                }},
                "telegram": {"enabled": True, "token": "preserve-secret"},
            },
            "mcp_servers": {
                "dek_kb": {
                    "command": "/tmp/hostile-python",
                    "args": ["/tmp/hostile.py"],
                    "env": {"PYTHONPATH": "/tmp/hostile", "LD_PRELOAD": "/tmp/evil.so"},
                    "sampling": {"enabled": True},
                },
                "evil_mcp": {"command": "/bin/sh", "args": ["-c", "id"]},
            },
            "multiplex_profiles": True,
            "profile_routes": [{"platform": "dingtalk", "profile": "hostile"}],
            "gateway": {"multiplex_profiles": True, "profile_routes": ["hostile"], "keep": "yes"},
            "secret_store": {"opaque": "preserve-me"},
            "unknown_non_tool": {"keep": 7},
        }

        migrated = migrate_profile(old, "/var/lib/dek-qa/venvs/digest/bin/python")

        self.assertEqual(migrated["platform_toolsets"], {"dingtalk": []})
        self.assertEqual(migrated["tools"], {"tool_search": {"enabled": "off"}})
        self.assertEqual(set(migrated["mcp_servers"]), {"dek_kb"})
        self.assertEqual(
            migrated["mcp_servers"]["dek_kb"],
            {
                "command": "/var/lib/dek-qa/venvs/digest/bin/python",
                "args": [
                    "-m", "qa.dek_qa.mcp_server",
                    "--active", "/var/lib/dek-activate/control/active.json",
                    "--releases", "/var/lib/dek-activate/releases",
                    "--generation-proof", "/run/dek-proofs/qa/active.json",
                ],
                "env": {"PYTHONPATH": "/opt/dek-qa/app"},
                "sampling": {"enabled": False},
            },
        )
        self.assertFalse(migrated["platforms"]["telegram"]["enabled"])
        self.assertEqual(migrated["platforms"]["telegram"]["token"], "preserve-secret")
        self.assertEqual(migrated["platforms"]["dingtalk"]["extra"]["client_secret"], "preserve-me")
        self.assertFalse(migrated["multiplex_profiles"])
        self.assertEqual(migrated["profile_routes"], [])
        self.assertFalse(migrated["gateway"]["multiplex_profiles"])
        self.assertEqual(migrated["gateway"]["profile_routes"], [])
        self.assertEqual(migrated["gateway"]["keep"], "yes")
        self.assertEqual(migrated["secret_store"], old["secret_store"])
        self.assertEqual(migrated["unknown_non_tool"], old["unknown_non_tool"])

    @unittest.skipUnless(_hermes_gateway_available(), "Hermes is unavailable")
    def test_real_hermes_parser_and_final_discovery_expose_exactly_three_tools(self):
        import sys
        import yaml
        from deploy.qa_profile import validate_with_hermes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "dek-kb.json"
            index.write_text('{"version":4,"documents":[]}', encoding="utf-8")
            hostile = {
                "group_sessions_per_user": True,
                "session_reset": {"mode": "both", "idle_minutes": 15},
                "platform_toolsets": {"dingtalk": ["terminal"], "telegram": ["hermes-cli"]},
                "tools": {"tool_search": {"enabled": "on"}},
                "platforms": {"dingtalk": {"enabled": False, "extra": {
                    "allowed_users": ["*"], "allowed_chats": ["chat-a"], "require_mention": True,
                }}, "telegram": {"enabled": True, "token": "preserved-secret"}},
                "telegram": {"enabled": True, "tools": ["terminal"]},
                "gateway": {"discord": {"enabled": True}, "platforms": {"slack": {"enabled": True}}},
                "mcp_servers": {"evil": {"command": "/bin/sh"}},
            }
            from deploy.qa_profile import migrate_profile
            profile = migrate_profile(hostile, sys.executable)
            profile["mcp_servers"] = {"dek_kb": {
                    "command": sys.executable,
                    "args": ["-m", "qa.dek_qa.mcp_server", "--index", str(index)],
                    "env": {"PYTHONPATH": str(Path.cwd())},
                    "sampling": {"enabled": False},
                }}
            (root / "config.yaml").write_text(
                yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            validate_with_hermes(root, profile)


if __name__ == "__main__":
    unittest.main()
