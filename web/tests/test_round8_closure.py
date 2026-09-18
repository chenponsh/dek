import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deploy.activator import ActivatorConfig
from deploy.rollback import verify_enablement
from deploy.seed_release import assert_seed_monotonic, global_high_sequence


def _seed_descriptor():
    return {"schema_version": 2, "sequence": 1, "nonce": "seed-12345678", "generation": "seed",
            "previous_generation": None, "commit": "1" * 40, "tree": "2" * 40,
            "bundle_sha256": "3" * 64,
            "artifacts": {"dek-kb.json": "4" * 64, "site/index.html": "5" * 64}}


class SeedMonotonicRound8Tests(unittest.TestCase):
    def _cfg(self, root):
        cfg = ActivatorConfig.under(root)
        cfg.prepare()
        return cfg

    def _guard(self, cfg, descriptor):
        assert_seed_monotonic(cfg.active, cfg.releases, cfg.journal, cfg.spent, cfg.outcomes, descriptor)

    def test_fresh_state_allows_seed_sequence_one(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            self._guard(cfg, _seed_descriptor())  # must not raise
            self.assertIsNone(global_high_sequence(cfg.active, cfg.releases, cfg.journal, cfg.spent, cfg.outcomes))

    def test_release_dir_higher_sequence_blocks_reseed_after_active_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            (cfg.releases / "g2").mkdir(parents=True)
            (cfg.releases / "g2" / "release.json").write_text(
                json.dumps({"schema_version": 2, "sequence": 2, "generation": "g2"}))
            # active.json absent: exactly the reported downgrade repro
            self.assertFalse(cfg.active.exists())
            with self.assertRaisesRegex(SystemExit, "downgrade"):
                self._guard(cfg, _seed_descriptor())

    def test_spent_higher_sequence_blocks_reseed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            (cfg.spent / "spent-nonce.json").write_text(json.dumps({"sequence": 3, "generation": "g3"}))
            with self.assertRaisesRegex(SystemExit, "downgrade"):
                self._guard(cfg, _seed_descriptor())

    def test_journal_higher_sequence_blocks_reseed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            (cfg.journal / "00000000000000000004-nonce.json").write_text(
                json.dumps({"status": "succeeded", "requested": {"sequence": 4, "nonce": "n4"},
                            "previous": {"sequence": 3}}))
            with self.assertRaisesRegex(SystemExit, "downgrade"):
                self._guard(cfg, _seed_descriptor())

    def test_outcome_higher_sequence_blocks_reseed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            (cfg.outcomes / "outcome-nonce.json").write_text(json.dumps({"status": "succeeded", "sequence": 5}))
            with self.assertRaisesRegex(SystemExit, "downgrade"):
                self._guard(cfg, _seed_descriptor())

    def test_existing_sequence_one_allows_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            cfg.active.write_text(json.dumps(_seed_descriptor()))
            self._guard(cfg, _seed_descriptor())  # high == 1, replay allowed

    def test_unreadable_committed_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            (cfg.journal / "corrupt.json").write_text("{not json")
            with self.assertRaisesRegex(SystemExit, "unreadable"):
                self._guard(cfg, _seed_descriptor())


class EnablementVerifyRound8Tests(unittest.TestCase):
    def test_verify_queries_every_unit_and_passes_on_exact_match(self):
        states = {"a.service": "enabled", "b.service": "enabled-runtime", "c.service": "disabled",
                  "d.service": "masked", "e.service": "masked-runtime", "f.service": "static",
                  "g.service": "indirect", "h.service": "generated", "i.service": "transient",
                  "j.service": "alias"}
        queried = []

        def runner(*argv):
            queried.append(argv[-1])
            return SimpleNamespace(stdout=states[argv[-1]] + "\n")

        verify_enablement(states, runner=runner)
        self.assertEqual(sorted(queried), sorted(states.keys()))

    def test_verify_fails_closed_and_reports_mismatch(self):
        states = {"dek-web.service": "enabled", "dek-activator.service": "static"}
        observed = {"dek-web.service": "enabled", "dek-activator.service": "generated"}

        def runner(*argv):
            unit = argv[-1]
            return SimpleNamespace(stdout=observed[unit] + "\n")

        with self.assertRaisesRegex(SystemExit, "dek-activator.service"):
            verify_enablement(states, runner=runner)


if __name__ == "__main__":
    unittest.main()
