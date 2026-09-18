"""Regression test for a real production bug in Activator.activate(): the
releases/ directory is setgid so dek-web (via the dek-release-read group)
can read published content, but shutil.copytree's final copystat(source,
staging) call matches staging's mode -- including the setgid bit -- to the
candidate source's mode, which is never setgid. release.json/release.lock
are written into staging AFTER that copystat call, so they silently lose
the inherited group and land owned by whatever group the activator process
happened to have -- which is NOT dek-release-read (dek-activator is not a
member of that group; it only gets it via directory-setgid inheritance).
dek-web then can't open release.lock/release.json and every request 503s.
This reproduced for real on this session's first live seed-release
activation and would have recurred on every future publish.
"""
import grp
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deploy.activator import Activator, ActivatorConfig


@unittest.skipUnless(os.geteuid() == 0, "setgid group-inheritance semantics require root to set up")
class ActivateReleaseGroupInheritanceTests(unittest.TestCase):
    def test_release_lock_and_release_json_inherit_the_releases_directory_group(self):
        own_gid = os.getegid()
        target_gid = next(g.gr_gid for g in grp.getgrall() if g.gr_gid not in (0, own_gid))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ActivatorConfig.under(root); config.prepare()
            os.chown(config.releases, os.getuid(), target_gid)
            os.chmod(config.releases, 0o2750)  # setgid, matching production's dek-release-read layout

            activator = Activator(config, proof_reader=lambda kind, expected: expected)
            source = activator.test_release("gen-0000001", sequence=1)
            self.assertNotEqual(os.stat(source).st_gid, target_gid, "fixture sanity: candidate source must not already carry the target group")

            activator.activate(source)

            target = config.releases / "gen-0000001"
            self.assertEqual(os.stat(target / "release.lock").st_gid, target_gid)
            self.assertEqual(os.stat(target / "release.json").st_gid, target_gid)
            active = json.loads(config.active.read_text())
            self.assertEqual(active["generation"], "gen-0000001")


if __name__ == "__main__":
    unittest.main()
