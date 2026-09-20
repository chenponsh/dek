from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

import yaml
from types import SimpleNamespace


@contextmanager
def _null_context():
    yield


class CandidateIngestionIsolationTests(unittest.TestCase):
    def test_real_cli_dispatch_enters_scheduled_run_with_no_publication(self):
        from ingestion.automation import cli

        observed = []
        with patch.object(cli, "ingestion_lock", side_effect=lambda _root: _null_context()), \
                patch.object(cli, "execute_scheduled", side_effect=lambda *, no_publication=False: observed.append(no_publication) or 0):
            self.assertEqual(0, cli.main(["scheduled-run", "--no-publication"]))
        self.assertEqual([True], observed)

    def test_no_publication_scheduled_run_writes_candidate_results_but_git_publish_is_unreachable(self):
        from ingestion.automation import cli

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "candidate.md"
            rough = root / "ingestion" / "rough" / "candidate.md"
            source.parent.mkdir(parents=True)
            rough.parent.mkdir(parents=True)
            source.write_text("old", encoding="utf-8")
            report = {
                "mode": "scheduled-run", "report": {"source/candidate.md": {"status": "updated_with_new"}},
                "rough_created": ["ingestion/rough/candidate.md"],
                "rough_sources": {"ingestion/rough/candidate.md": "source/candidate.md"},
                "planned_writes": ["ingestion/rough/candidate.md", "source/candidate.md"],
                "auto_write_paths": ["ingestion/rough/candidate.md", "source/candidate.md"],
                "blocking": False, "alerts": [],
            }
            writes = {source: "new", rough: "rough"}
            calls = []

            def fake_git(_root, *args):
                calls.append(args)
                if args and args[0] in {"commit", "push"}:
                    self.fail(f"publication command was reachable: {args[0]}")
                if args[:2] == ("rev-parse", "HEAD"):
                    return "abc"
                if args[:2] == ("rev-parse", "origin/main"):
                    return "abc"
                return ""

            def status_paths(_root):
                return {str(path.relative_to(root)) for path in writes}

            @contextmanager
            def fake_atomic(_root, pending):
                for path, content in pending.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                yield

            config = {"cde": {"sources": [{
                "path": "source/candidate.md", "auto_classified": True, "auto_ingest": True,
            }]}}
            with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), \
                    patch.object(cli, "inspect", return_value=(report, writes)), \
                    patch.object(cli, "reconcile_remote") as reconcile, patch.object(cli, "assert_git_safe"), \
                    patch.object(cli, "git_status_paths", side_effect=status_paths), \
                    patch.object(cli, "atomic_write_batch", side_effect=fake_atomic), \
                    patch.object(cli, "git", side_effect=fake_git):
                self.assertEqual(0, cli.execute_scheduled(no_publication=True))

            reconcile.assert_not_called()
            self.assertEqual("new", source.read_text(encoding="utf-8"))
            self.assertEqual("rough", rough.read_text(encoding="utf-8"))
            self.assertFalse(any(command and command[0] in {"add", "commit", "push"} for command in calls))

    def test_candidate_module_is_loaded_only_from_explicit_package_and_never_publishes(self):
        from deploy.source_ingest_entrypoint import load_ingestion_modules, publication_plan

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "candidate"
            automation = package / "ingestion" / "automation"
            automation.mkdir(parents=True)
            (package / "ingestion" / "__init__.py").write_text("", encoding="utf-8")
            (automation / "__init__.py").write_text("", encoding="utf-8")
            for name in ("cli", "core", "fetchers", "audit", "sources"):
                (automation / f"{name}.py").write_text(
                    ("ROOT = None\nAPPROVAL_PATH = None\ndef main(argv): return 0\n"
                     if name == "cli" else "MARKER = 'candidate'\n"),
                    encoding="utf-8",
                )
            modules = load_ingestion_modules(package)
            self.assertTrue(all(Path(module.__file__).is_relative_to(package) for module in modules))
            self.assertEqual((), publication_plan(pre_cutover=True, changed=True))
            self.assertEqual(("commit", "push", "bundle"), publication_plan(pre_cutover=False, changed=True))

    def test_runbook_candidate_names_package_root_and_pre_cutover_no_publish_mode(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]
        boundary = a6.index("# LIVE CUTOVER BEGINS")
        candidate = a6[:boundary]
        self.assertIn("--package-root /run/dek-package-check", candidate)
        self.assertIn("--pre-cutover-proof", candidate)
        self.assertNotIn("git commit", candidate)
        self.assertNotIn("git push", candidate)


class CandidateHermesWriteConfinementTests(unittest.TestCase):
    def test_hermes_candidate_environment_pins_every_temp_namespace_below_stage_state(self):
        from deploy.qa_profile import candidate_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            state = root / "acceptance-state"
            profile.mkdir()
            environment = candidate_environment(profile, state)
            self.assertEqual(str(state / "tmp"), environment["TMPDIR"])
            for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "TMPDIR"):
                self.assertTrue(Path(environment[key]).is_relative_to(root), key)
                self.assertTrue(Path(environment[key]).is_dir(), key)

    def test_staged_validation_rebinds_generation_proof_below_private_state_root(self):
        from deploy.qa_profile import validate_staged_with_hermes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile" / "config.yaml"
            profile.parent.mkdir()
            profile.write_text(yaml.safe_dump({
                "platforms": {"dingtalk": {"enabled": True, "extra": {
                    "allowed_users": ["*"], "allowed_chats": ["chat"], "require_mention": True,
                }}},
                "mcp_servers": {"dek_kb": {
                    "command": "/var/lib/dek-qa/venvs/" + "a" * 64 + "/bin/python",
                    "args": ["-m", "qa.dek_qa.mcp_server", "--active", "/var/lib/dek-activate/control/active.json",
                             "--releases", "/var/lib/dek-activate/releases", "--generation-proof", "/run/dek-proofs/qa/active.json"],
                    "env": {"PYTHONPATH": "/opt/dek-qa/app"},
                }},
            }), encoding="utf-8")
            state = root / "state"
            observed = {}
            with patch("deploy.qa_profile.validate_with_hermes", side_effect=lambda p, e, s=None: observed.update(profile=p, expected=e, state=s)):
                validate_staged_with_hermes(profile, "/stage/venv/bin/python", "/stage/package", state)
            args = observed["expected"]["mcp_servers"]["dek_kb"]["args"]
            proof = Path(args[args.index("--generation-proof") + 1])
            self.assertTrue(proof.is_relative_to(state))
            self.assertNotEqual(Path("/run/dek-proofs/qa/active.json"), proof)

    def test_a6_hermes_sandbox_has_no_live_writable_state(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]
        before = a6[:a6.index("# LIVE CUTOVER BEGINS")]
        for token in (
            'QA_ACCEPT_STATE="$STAGE_ROOT/qa/acceptance-state"',
            'QA_TMP="$QA_ACCEPT_STATE/tmp"',
            '--runtime-state-root "$QA_ACCEPT_STATE"',
            '--setenv=TMPDIR="$QA_TMP"',
            '--property=ReadOnlyPaths=/tmp /run',
            '--property=ReadWritePaths="$STAGE_ROOT/qa"',
            '--property=InaccessiblePaths=/run/dek-proofs',
            "candidate write confinement probe passed",
        ):
            self.assertIn(token, before)
        probe = before.index("candidate write confinement probe passed")
        assembly = before.index("qa_profile.py", probe)
        self.assertLess(probe, assembly)


class StrictAncestorValidationTests(unittest.TestCase):
    def test_writable_ancestor_is_rejected_unless_exact_test_exception_is_explicit(self):
        from deploy.install_components import _open_approved_root

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            unsafe = base / "unsafe"
            root = unsafe / "approved"
            root.mkdir(parents=True)
            os.chmod(unsafe, 0o777)
            with self.assertRaisesRegex(RuntimeError, "ancestor"):
                _open_approved_root(root, create=False)
            descriptor, _ = _open_approved_root(
                root, create=False,
                test_only_allow_unsafe_ancestors={Path(temporary).parent, unsafe},
            )
            os.close(descriptor)

    def test_journal_ancestor_and_post_open_symlink_replacement_cannot_escape(self):
        from deploy.install_components import open_secure_journal

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            safe = base / "safe"; safe.mkdir()
            journal = safe / "journal"
            pinned = open_secure_journal(journal, test_only_allow_unsafe_ancestors={Path(temporary).parent})
            outside = base / "outside"; outside.mkdir()
            parked = base / "parked"
            journal.rename(parked)
            journal.symlink_to(outside, target_is_directory=True)
            try:
                pinned.atomic_json("probe.json", {"ok": True})
            finally:
                pinned.close()
            self.assertTrue((parked / "probe.json").is_file())
            self.assertFalse((outside / "probe.json").exists())


class A6SingleTransactionTests(unittest.TestCase):
    @staticmethod
    def _fixture(base: Path):
        from deploy.a6_cutover import CutoverLayout, MemoryServiceManager

        stage = base / "stage"; live = base / "live"; units = base / "units"; journal = base / "journal"
        for path in (stage / "venv", stage / "profile", stage / "source", live, units):
            path.mkdir(parents=True)
        journal.mkdir(mode=0o700)
        for name in ("venv", "profile", "source"):
            (live / (name + "-old")).mkdir()
            (live / name).symlink_to(name + "-old")
            (stage / name / "candidate").write_text(name[0], encoding="utf-8")
        unit_names = ("dek-qa.service", "dek-source-ingest.service", "dek-source-ingest.timer")
        candidates = {}
        for name in unit_names:
            (units / name).write_text("old-" + name, encoding="utf-8")
            candidate = stage / (name + ".candidate")
            candidate.write_text("new-" + name, encoding="utf-8")
            candidates[name] = candidate
        manager = MemoryServiceManager({
            "dek-qa.service": (True, True),
            "dek-source-ingest.service": (False, False),
            "dek-source-ingest.timer": (True, True),
        })
        layout = CutoverLayout(
            stage_venv=stage / "venv", stage_profile=stage / "profile", stage_source=stage / "source",
            live_root=live, unit_root=units, unit_candidates=candidates, journal_dir=journal,
            test_only_allow_unsafe_ancestors={base.parent},
        )
        return layout, manager, unit_names

    def test_injected_failure_recovers_every_path_unit_and_service_contract(self):
        from deploy.a6_cutover import CutoverLayout, MemoryServiceManager, recover, run_cutover

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            layout, manager, unit_names = self._fixture(base)
            live, units, journal = layout.live_root, layout.unit_root, layout.journal_dir
            baseline = manager.contract()
            with self.assertRaisesRegex(RuntimeError, "injected"):
                run_cutover(layout, manager, fail_after="unit:dek-source-ingest.service")
            self.assertTrue((journal / "a6-cutover.json").exists())
            recover(layout, manager)
            self.assertEqual("venv-old", os.readlink(live / "venv"))
            self.assertEqual("profile-old", os.readlink(live / "profile"))
            self.assertEqual("source-old", os.readlink(live / "source"))
            self.assertEqual(baseline, manager.contract())
            self.assertTrue(all((units / name).read_text() == "old-" + name for name in unit_names))
            self.assertFalse((journal / "a6-cutover.json").exists())

    def test_every_mutation_boundary_is_recoverable_and_verified(self):
        from deploy.a6_cutover import recover, run_cutover

        steps = [
            *("service-stop:" + name for name in ("dek-qa.service", "dek-source-ingest.service", "dek-source-ingest.timer")),
            *(part + ":" + name for name in ("venv", "profile", "source") for part in ("asset-move", "link")),
            *("unit:" + name for name in ("dek-qa.service", "dek-source-ingest.service", "dek-source-ingest.timer")),
            "daemon-reload", "service-contract",
        ]
        for step in steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temporary:
                layout, manager, unit_names = self._fixture(Path(temporary))
                before = manager.contract()
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    run_cutover(layout, manager, fail_after=step)
                recover(layout, manager)
                self.assertEqual(before, manager.contract())
                self.assertFalse((layout.journal_dir / "a6-cutover.json").exists())
                self.assertTrue((layout.journal_dir / "a6-cutover.recovered.json").is_file())

    def test_verified_transaction_remains_recoverable_until_explicit_finalize(self):
        from deploy.a6_cutover import finalize, run_cutover

        with tempfile.TemporaryDirectory() as temporary:
            layout, manager, _ = self._fixture(Path(temporary))
            run_cutover(layout, manager)
            self.assertTrue((layout.journal_dir / "a6-cutover.json").is_file())
            finalize(layout, manager)
            self.assertFalse((layout.journal_dir / "a6-cutover.json").exists())
            self.assertTrue((layout.journal_dir / "a6-cutover.finalized.json").is_file())

    def test_runbook_uses_one_tested_cutover_and_one_recovery_command(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        a6 = text[text.index("## Checkpoint A6"):text.index("## Exact manual rollback")]
        after = a6[a6.index("# LIVE CUTOVER BEGINS"):]
        self.assertIn("a6_cutover.py apply", after)
        self.assertIn("a6_cutover.py recover", after)
        self.assertNotIn('mv "$QA_CANDIDATE"', after)
        self.assertNotIn("--cutover-source-ingest", after)

    def test_sigkill_after_legacy_directory_rename_recovers_and_repeated_recover_is_safe(self):
        from deploy.a6_cutover import recover, run_cutover

        with tempfile.TemporaryDirectory() as temporary:
            layout, manager, _ = self._fixture(Path(temporary))
            link = layout.live_root / "venv"
            link.unlink()
            (link / "legacy.txt").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                run_cutover(layout, manager, fail_after_mutation="legacy-rename:venv")
            recover(layout, manager)
            recover(layout, manager)
            self.assertTrue((link / "legacy.txt").is_dir())
            self.assertFalse(link.is_symlink())

    def test_every_recovery_mutation_boundary_survives_unrecorded_interrupt_and_double_retry(self):
        from deploy.a6_cutover import recover, run_cutover

        steps = [
            *("unit:" + name for name in ("dek-qa.service", "dek-source-ingest.service", "dek-source-ingest.timer")),
            *(part + ":" + name for name in ("source", "profile", "venv") for part in ("link", "candidate")),
            "daemon-reload", "services",
        ]
        for step in steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temporary:
                layout, manager, unit_names = self._fixture(Path(temporary))
                baseline = manager.contract()
                run_cutover(layout, manager)
                with self.assertRaisesRegex(RuntimeError, "injected recovery"):
                    recover(layout, manager, fail_after_mutation=step)
                recover(layout, manager)
                recover(layout, manager)
                self.assertEqual(baseline, manager.contract())
                self.assertEqual("venv-old", os.readlink(layout.live_root / "venv"))
                self.assertTrue(all(
                    (layout.unit_root / name).read_text() == "old-" + name for name in unit_names
                ))

    def test_finalize_is_durable_and_idempotent_but_never_existed_still_fails(self):
        from deploy.a6_cutover import finalize, run_cutover

        for boundary in ("completion-marker", "journal-unlink"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                layout, manager, _ = self._fixture(Path(temporary))
                run_cutover(layout, manager)
                with self.assertRaisesRegex(RuntimeError, "injected finalize"):
                    finalize(layout, manager, fail_after_mutation=boundary)
                finalize(layout, manager)
                finalize(layout, manager)
                self.assertFalse((layout.journal_dir / "a6-cutover.json").exists())
                self.assertTrue((layout.journal_dir / "a6-cutover.finalized.json").is_file())

        with tempfile.TemporaryDirectory() as temporary:
            layout, manager, _ = self._fixture(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "no verified A6 transaction"):
                finalize(layout, manager)


class A6ExactSystemdStateTests(unittest.TestCase):
    def test_restore_plan_clears_both_scopes_before_rebuilding_exact_mutable_state(self):
        from deploy.a6_cutover import unit_file_restore_plan

        unit = "legacy.service"
        fragment = "/opt/legacy/legacy.service"
        reset = [
            ("unmask", unit),
            ("unmask", "--runtime", unit),
            ("disable", unit),
            ("disable", "--runtime", unit),
        ]
        expected = {
            "enabled": (reset + [("enable", unit)], []),
            "enabled-runtime": (reset + [("enable", "--runtime", unit)], []),
            "masked": (reset, [("mask", "--force", unit)]),
            "masked-runtime": (reset, [("mask", "--runtime", unit)]),
            "linked": (reset + [("link", fragment)], []),
            "linked-runtime": (reset + [("link", "--runtime", fragment)], []),
        }
        for state, plan in expected.items():
            with self.subTest(state=state):
                self.assertEqual(plan, unit_file_restore_plan(unit, state, fragment))

    def test_all_documented_unit_file_states_have_an_explicit_restore_or_fail_closed_policy(self):
        from deploy.a6_cutover import UNIT_FILE_STATES, unit_file_restore_plan

        expected = {
            "enabled", "enabled-runtime", "linked", "linked-runtime", "alias",
            "masked", "masked-runtime", "static", "disabled", "indirect",
            "generated", "transient", "bad",
        }
        self.assertEqual(expected, UNIT_FILE_STATES)
        unit = "example.service"
        fragment = "/opt/example/example.service"
        plans = {state: unit_file_restore_plan(unit, state, fragment) for state in expected - {"bad"}}
        self.assertIn(("enable", "--runtime", unit), plans["enabled-runtime"][0])
        self.assertEqual(("mask", "--runtime", unit), plans["masked-runtime"][1][-1])
        self.assertIn(("link", fragment), plans["linked"][0])
        self.assertIn(("link", "--runtime", fragment), plans["linked-runtime"][0])
        for intrinsic in ("static", "indirect", "alias", "generated", "transient"):
            self.assertEqual(([], []), plans[intrinsic])
        with self.assertRaisesRegex(RuntimeError, "bad"):
            unit_file_restore_plan(unit, "bad", fragment)

    def test_contract_captures_unit_file_active_substate_and_fragment_without_loss(self):
        from deploy.a6_cutover import SystemdServiceManager

        properties = {
            "a.service": ("enabled-runtime", "active", "running", "/etc/systemd/system/a.service"),
            "b.service": ("masked-runtime", "failed", "failed", "/etc/systemd/system/b.service"),
            "c.service": ("linked-runtime", "inactive", "dead", "/opt/c.service"),
        }

        def runner(argv, **_kwargs):
            unit = argv[2]
            state, active, sub, fragment = properties[unit]
            return SimpleNamespace(
                returncode=0,
                stdout=f"UnitFileState={state}\nActiveState={active}\nSubState={sub}\nFragmentPath={fragment}\n",
                stderr="",
            )

        manager = SystemdServiceManager(properties, runner=runner)
        self.assertEqual({
            name: {"unit_file_state": values[0], "active_state": values[1],
                   "sub_state": values[2], "fragment_path": values[3]}
            for name, values in properties.items()
        }, manager.contract())

    def test_unreconstructable_forward_states_fail_before_first_stop(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager(())
        for state in ("linked", "linked-runtime", "generated", "transient", "bad"):
            contract = {"x.service": {
                "unit_file_state": state, "active_state": "inactive",
                "sub_state": "dead", "fragment_path": "/opt/x.service",
            }}
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "cannot safely preserve"):
                manager.validate_apply(contract)

    def test_any_failed_activity_contract_fails_closed_before_first_stop(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager(())
        for active, sub in (
            ("failed", "failed"),
            ("failed", "dead"),
            ("inactive", "failed"),
            ("active", "failed"),
        ):
            contract = {"x.service": {
                "unit_file_state": "enabled-runtime", "active_state": active,
                "sub_state": sub, "fragment_path": "/etc/systemd/system/x.service",
            }}
            with self.subTest(active=active, sub=sub), self.assertRaisesRegex(
                    RuntimeError, "cannot safely preserve systemd activity"):
                manager.validate_apply(contract)

    def test_failed_apply_contract_preflight_precedes_completion_marker_mutation(self):
        from deploy.a6_cutover import SystemdServiceManager, recover, run_cutover

        with tempfile.TemporaryDirectory() as temporary:
            layout, memory, _ = A6SingleTransactionTests._fixture(Path(temporary))
            run_cutover(layout, memory)
            recover(layout, memory)
            marker = layout.journal_dir / "a6-cutover.recovered.json"
            marker_before = marker.read_bytes()
            stops = []
            manager = SystemdServiceManager(())
            manager.contract = lambda: {"legacy.service": {
                "unit_file_state": "enabled-runtime", "active_state": "failed",
                "sub_state": "failed", "fragment_path": "/etc/systemd/system/legacy.service",
            }}
            manager.stop = stops.append

            with self.assertRaisesRegex(RuntimeError, "cannot safely preserve systemd activity"):
                run_cutover(layout, manager)

            self.assertEqual([], stops)
            self.assertEqual(marker_before, marker.read_bytes())

    def test_activity_plan_preserves_active_inactive_and_failed_semantics(self):
        from deploy.a6_cutover import activity_restore_plan

        self.assertEqual((["reset-failed"], ["start"], False), activity_restore_plan("active", "running"))
        self.assertEqual((["stop", "reset-failed"], [], False), activity_restore_plan("inactive", "dead"))
        for active, sub in (
            ("failed", "failed"), ("failed", "dead"), ("inactive", "failed"),
            ("activating", "start"), ("deactivating", "stop-sigterm"),
        ):
            with self.subTest(active=active, sub=sub), self.assertRaisesRegex(RuntimeError, "cannot safely restore"):
                activity_restore_plan(active, sub)

    def test_restore_executes_runtime_enablement_and_mask_then_exactly_rechecks(self):
        from deploy.a6_cutover import SystemdServiceManager

        contract = {
            "runtime.service": {
                "unit_file_state": "enabled-runtime", "active_state": "active",
                "sub_state": "running", "fragment_path": "/etc/systemd/system/runtime.service",
            },
            "masked.timer": {
                "unit_file_state": "masked-runtime", "active_state": "active",
                "sub_state": "waiting", "fragment_path": "/etc/systemd/system/masked.timer",
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs.get("check")))
            if argv[1] == "show":
                state = contract[argv[2]]
                return SimpleNamespace(returncode=0, stderr="", stdout="".join(
                    f"{key}={state[field]}\n" for key, field in (
                        ("UnitFileState", "unit_file_state"), ("ActiveState", "active_state"),
                        ("SubState", "sub_state"), ("FragmentPath", "fragment_path"),
                    )
                ))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        manager = SystemdServiceManager(contract, runner=runner)
        manager.restore(contract)
        commands = [item[0][1:] for item in calls if item[0][1] != "show"]
        self.assertIn(("enable", "--runtime", "runtime.service"), commands)
        self.assertIn(("start", "runtime.service"), commands)
        runtime_start = next(item for item in calls if item[0][1:] == ("start", "runtime.service"))
        self.assertTrue(runtime_start[1])
        self.assertIn(("mask", "--runtime", "masked.timer"), commands)

    def test_old_failed_journal_recovery_requires_exact_manual_rollback_without_mutation(self):
        from deploy.a6_cutover import SystemdServiceManager, recover, run_cutover, tree_manifest

        with tempfile.TemporaryDirectory() as temporary:
            layout, memory, _ = A6SingleTransactionTests._fixture(Path(temporary))
            run_cutover(layout, memory)
            journal = layout.journal_dir / "a6-cutover.json"
            value = json.loads(journal.read_text(encoding="utf-8"))
            value["services_before"] = {"legacy.service": {
                "unit_file_state": "linked", "active_state": "failed",
                "sub_state": "failed", "fragment_path": "/opt/legacy/legacy.service",
            }}
            journal.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            before_tree = tree_manifest(layout.manifest_paths())
            before_journal = journal.read_bytes()

            def forbidden_runner(*_args, **_kwargs):
                self.fail("recover invoked systemctl before rejecting the old failed contract")

            manager = SystemdServiceManager((), runner=forbidden_runner)
            with self.assertRaisesRegex(RuntimeError, "Exact manual rollback required"):
                recover(layout, manager)

            self.assertEqual(before_tree, tree_manifest(layout.manifest_paths()))
            self.assertEqual(before_journal, journal.read_bytes())

    def test_old_unrestorable_unit_file_contract_fails_before_any_recovery_mutation(self):
        from deploy.a6_cutover import SystemdServiceManager, recover, run_cutover, tree_manifest

        cases = (
            ("bad", "/opt/legacy/legacy.service"),
            ("future-state", "/opt/legacy/legacy.service"),
            ("linked", ""),
            ("linked-runtime", "relative/legacy.service"),
            ("linked", "/opt/legacy/../secret/legacy.service"),
            ("linked", "/opt/legacy/\ud800.service"),
            ("linked-runtime", "/opt/legacy/c1\x85.service"),
        )
        diagnostic = (
            "Exact manual rollback required: A6 journal contains an "
            "unreconstructable systemd restore contract"
        )
        for unit_file_state, fragment_path in cases:
            with self.subTest(unit_file_state=unit_file_state, fragment_path=fragment_path), \
                    tempfile.TemporaryDirectory() as temporary:
                layout, memory, _ = A6SingleTransactionTests._fixture(Path(temporary))
                run_cutover(layout, memory)
                journal = layout.journal_dir / "a6-cutover.json"
                value = json.loads(journal.read_text(encoding="utf-8"))
                value["services_before"] = {"legacy.service": {
                    "unit_file_state": unit_file_state, "active_state": "inactive",
                    "sub_state": "dead", "fragment_path": fragment_path,
                }}
                journal.write_text(
                    json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                before_tree = tree_manifest(layout.manifest_paths())
                before_journal = journal.read_bytes()
                systemctl_calls = []

                def forbidden_runner(*args, **_kwargs):
                    systemctl_calls.append(args)
                    self.fail("recover invoked systemctl before rejecting the old unit-file contract")

                manager = SystemdServiceManager((), runner=forbidden_runner)
                with self.assertRaises(RuntimeError) as raised:
                    recover(layout, manager)

                self.assertEqual(diagnostic, str(raised.exception))
                if fragment_path:
                    self.assertNotIn(fragment_path, str(raised.exception))
                self.assertEqual(before_tree, tree_manifest(layout.manifest_paths()))
                self.assertEqual(before_journal, journal.read_bytes())
                self.assertEqual([], systemctl_calls)

    def test_recover_preflight_accepts_canonical_absolute_linked_fragment_paths(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager((), runner=lambda *_args, **_kwargs: self.fail("unexpected systemctl"))
        manager.validate_recover({
            "persistent.service": {
                "unit_file_state": "linked", "active_state": "inactive",
                "sub_state": "dead", "fragment_path": "/opt/legacy/persistent.service",
            },
            "runtime.service": {
                "unit_file_state": "linked-runtime", "active_state": "active",
                "sub_state": "running", "fragment_path": "/run/legacy/runtime.service",
            },
        })

    def test_linked_fragment_path_rejects_unsafe_unicode_and_noncanonical_paths(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager((), runner=lambda *_args, **_kwargs: self.fail("unexpected systemctl"))
        unsafe = (
            "/opt/legacy/\ud800.service",
            "/opt/legacy/\udc80.service",
            "/opt/legacy/nul\x00.service",
            "/opt/legacy/c1\x85.service",
            "/opt//legacy.service",
            "/opt/legacy/./x.service",
            "/opt/legacy/../x.service",
            "/opt/legacy/x.service/",
            "relative/legacy.service",
        )
        diagnostic = (
            "Exact manual rollback required: A6 journal contains an "
            "unreconstructable systemd restore contract"
        )
        for state in ("linked", "linked-runtime"):
            for fragment_path in unsafe:
                with self.subTest(state=state, path=ascii(fragment_path)):
                    with self.assertRaises(RuntimeError) as raised:
                        manager.validate_recover({"legacy.service": {
                            "unit_file_state": state, "active_state": "inactive",
                            "sub_state": "dead", "fragment_path": fragment_path,
                        }})
                    self.assertEqual(diagnostic, str(raised.exception))
                    self.assertNotIn("legacy/", str(raised.exception))

    def test_linked_fragment_path_accepts_encodable_canonical_international_path(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager((), runner=lambda *_args, **_kwargs: self.fail("unexpected systemctl"))
        manager.validate_recover({"international.service": {
            "unit_file_state": "linked-runtime", "active_state": "inactive",
            "sub_state": "dead", "fragment_path": "/opt/旧版/服务.service",
        }})

    def test_fragment_filesystem_encoding_failure_uses_stable_non_sensitive_diagnostic(self):
        from deploy.a6_cutover import SystemdServiceManager

        manager = SystemdServiceManager((), runner=lambda *_args, **_kwargs: self.fail("unexpected systemctl"))
        with patch("deploy.a6_cutover.os.fsencode", side_effect=UnicodeEncodeError("ascii", "x", 0, 1, "no")):
            with self.assertRaises(RuntimeError) as raised:
                manager.validate_recover({"legacy.service": {
                    "unit_file_state": "linked", "active_state": "inactive",
                    "sub_state": "dead", "fragment_path": "/opt/敏感/legacy.service",
                }})
        self.assertEqual(
            "Exact manual rollback required: A6 journal contains an "
            "unreconstructable systemd restore contract",
            str(raised.exception),
        )
        self.assertNotIn("敏感", str(raised.exception))


class ExactManualRollbackUnitStateTests(unittest.TestCase):
    def test_snapshot_records_fragment_paths_for_linked_state_and_is_parseable(self):
        from deploy.rollback import parse_enablement, write_enablement_snapshot

        outputs = {
            "dek-a.service": "UnitFileState=enabled-runtime\nFragmentPath=/etc/systemd/system/dek-a.service\n",
            "dek-b.service": "UnitFileState=linked\nFragmentPath=/opt/旧版/dek-b.service\n",
        }

        def runner(argv, **_kwargs):
            if argv[1] == "list-unit-files":
                return SimpleNamespace(stdout="dek-a.service enabled-runtime enabled\ndek-b.service linked enabled\n")
            return SimpleNamespace(stdout=outputs[argv[2]])

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "unit-enablement.before"
            write_enablement_snapshot(output, runner=runner)
            self.assertEqual({
                "dek-a.service": {"unit_file_state": "enabled-runtime", "fragment_path": "/etc/systemd/system/dek-a.service"},
                "dek-b.service": {"unit_file_state": "linked", "fragment_path": "/opt/旧版/dek-b.service"},
            }, parse_enablement(output))
            self.assertEqual(0o600, output.stat().st_mode & 0o777)

    def test_snapshot_accepts_a_path_unit_alongside_stage_b_services_and_timers(self):
        """STAGE_B in rollback.py includes systemd .path units (the reviewer
        manual-publish/manual-ingest trigger watchers) -- the enablement
        snapshot must be able to capture and restore their state too, not
        only .service/.timer units."""
        from deploy.rollback import parse_enablement, write_enablement_snapshot

        outputs = {
            "dek-review-publish-manual.path": "UnitFileState=enabled\nFragmentPath=/etc/systemd/system/dek-review-publish-manual.path\n",
        }

        def runner(argv, **_kwargs):
            if argv[1] == "list-unit-files":
                return SimpleNamespace(stdout="dek-review-publish-manual.path enabled enabled\n")
            return SimpleNamespace(stdout=outputs[argv[2]])

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "unit-enablement.before"
            write_enablement_snapshot(output, runner=runner)
            self.assertEqual({
                "dek-review-publish-manual.path": {"unit_file_state": "enabled",
                                                    "fragment_path": "/etc/systemd/system/dek-review-publish-manual.path"},
            }, parse_enablement(output))

    def test_snapshot_accepts_a_bare_template_unit_without_a_failing_show_call(self):
        """A bare template unit (e.g. dek-source-ingest-alert@.service, which
        is really installed on this host) has no single invocation identity;
        `systemctl show` on it fails outright ('neither a valid invocation ID
        nor unit name') rather than returning empty properties. The snapshot
        must fall back to trusting list-unit-files for such units instead of
        crashing the whole capture."""
        from deploy.rollback import parse_enablement, write_enablement_snapshot

        def runner(argv, **_kwargs):
            if argv[1] == "list-unit-files":
                return SimpleNamespace(stdout="dek-source-ingest-alert@.service static -\n")
            raise subprocess.CalledProcessError(1, argv, output="",
                stderr="Failed to get properties: Unit name dek-source-ingest-alert@.service "
                       "is neither a valid invocation ID nor unit name.\n")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "unit-enablement.before"
            write_enablement_snapshot(output, runner=runner)
            self.assertEqual({
                "dek-source-ingest-alert@.service": {"unit_file_state": "static", "fragment_path": ""},
            }, parse_enablement(output))

    def test_manual_rollback_uses_shared_exact_plans_for_all_mutable_states(self):
        from deploy.rollback import build_enablement_restore_plan

        fragment = "/opt/旧版/legacy.service"
        states = {
            "a.service": {"unit_file_state": "enabled", "fragment_path": ""},
            "b.service": {"unit_file_state": "enabled-runtime", "fragment_path": ""},
            "c.service": {"unit_file_state": "masked", "fragment_path": ""},
            "d.service": {"unit_file_state": "masked-runtime", "fragment_path": ""},
            "e.service": {"unit_file_state": "linked", "fragment_path": fragment},
            "f.service": {"unit_file_state": "linked-runtime", "fragment_path": fragment},
        }
        commands = build_enablement_restore_plan(states)
        for unit, state in ((name, value["unit_file_state"]) for name, value in states.items()):
            reset = [
                ("systemctl", "unmask", unit),
                ("systemctl", "unmask", "--runtime", unit),
                ("systemctl", "disable", unit),
                ("systemctl", "disable", "--runtime", unit),
            ]
            start = next(index for index, command in enumerate(commands) if command == reset[0])
            self.assertEqual(reset, commands[start:start + 4], state)
        self.assertIn(("systemctl", "enable", "--runtime", "b.service"), commands)
        self.assertNotIn(("systemctl", "enable", "b.service"), commands)
        self.assertIn(("systemctl", "mask", "--runtime", "d.service"), commands)
        self.assertIn(("systemctl", "link", fragment), commands)
        self.assertIn(("systemctl", "link", "--runtime", fragment), commands)

    def test_manual_rollback_preflight_rejects_unreconstructable_state_without_runner_calls(self):
        from deploy.rollback import build_enablement_restore_plan, restore_enablement

        for state, fragment_path in (
            ("linked-runtime", "relative/legacy.service"),
            ("generated", ""),
            ("transient", ""),
        ):
            states = {"legacy.service": {
                "unit_file_state": state, "fragment_path": fragment_path,
            }}
            with self.subTest(state=state):
                with self.assertRaisesRegex(SystemExit, "cannot reconstruct exact unit enablement"):
                    build_enablement_restore_plan(states)
                calls = []
                with self.assertRaisesRegex(SystemExit, "cannot reconstruct exact unit enablement"):
                    restore_enablement(states, runner=lambda *argv: calls.append(argv))
                self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
