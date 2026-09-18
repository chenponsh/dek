"""Regression test for a real production outage: activating the actual
wiki content (1069 markdown source files -> 1188 built artifacts) produces
an active.json around 200KB, but both consumers of that file capped reads
at a hardcoded 65536 bytes -- ActiveSite.pin() in web/app.py (raising
"active generation is not a bounded regular file", surfaced to users as a
503) and Activator._read_active() in deploy/activator.py (which would have
made the activator itself unable to read back its own committed state on
the very next publish). Neither bound had ever been exercised against
realistic artifact-count data before the real seed release was installed.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deploy.activator import Activator, ActivatorConfig, atomic_json


def _padded_descriptor(*, target_size: int) -> dict:
    """A schema-valid descriptor whose `artifacts` map is padded with
    synthetic site/ entries until the canonical JSON reaches ~target_size
    bytes -- standing in for a real release with many built pages."""
    base = {
        "schema_version": 2, "sequence": 1, "nonce": "nonce-0000001",
        "generation": "gen-0000001", "previous_generation": None,
        "commit": "1" * 40, "tree": "2" * 40, "bundle_sha256": "3" * 64,
        "artifacts": {"dek-kb.json": "4" * 64, "site/index.html": "5" * 64},
    }
    index = 0
    while len(json.dumps(base, sort_keys=True, separators=(",", ":"))) < target_size:
        base["artifacts"][f"site/source/page-{index:06d}.html"] = format(index, "064x")
        index += 1
    return base


class ActivatorReadActiveSizeBoundTests(unittest.TestCase):
    def test_read_active_accepts_a_realistic_production_sized_descriptor(self):
        """~200KB, matching the real seed release's actual active.json size."""
        descriptor = _padded_descriptor(target_size=200_000)
        with tempfile.TemporaryDirectory() as temporary:
            config = ActivatorConfig.under(Path(temporary)); config.prepare()
            atomic_json(config.active, descriptor)
            activator = Activator(config, proof_reader=lambda *_: {})
            self.assertEqual(activator._read_active(), descriptor)

    def test_read_active_still_rejects_an_absurdly_oversized_file(self):
        """The cap must still exist -- just sized for real data, not zero."""
        with tempfile.TemporaryDirectory() as temporary:
            config = ActivatorConfig.under(Path(temporary)); config.prepare()
            config.active.write_bytes(b"{" + b" " * (9 * 1024 * 1024) + b"}")
            activator = Activator(config, proof_reader=lambda *_: {})
            from deploy.activator import FatalActivationError
            with self.assertRaises(FatalActivationError):
                activator._read_active()


class ActiveSitePinSizeBoundTests(unittest.TestCase):
    def _build_release_tree(self, root: Path, descriptor: dict):
        releases = root / "releases"; releases.mkdir()
        active = root / "control" / "active.json"; active.parent.mkdir(parents=True)
        release_dir = releases / descriptor["generation"]
        (release_dir / "site").mkdir(parents=True)
        (release_dir / "site" / "index.html").write_text("<html></html>", encoding="utf-8")
        (release_dir / "release.json").write_text(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        (release_dir / "release.lock").touch()
        active.write_text(json.dumps(descriptor, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return releases, active

    def test_pin_accepts_a_realistic_production_sized_active_json(self):
        from web.app import ActiveSite
        descriptor = _padded_descriptor(target_size=200_000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases, active = self._build_release_tree(root, descriptor)
            site = ActiveSite(active, releases)
            pin = site.pin()
            try:
                self.assertEqual(pin.metadata, descriptor)
            finally:
                pin.close()


if __name__ == "__main__":
    unittest.main()
