import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.rollback import normalize_inventory, restore_enablement, validate_archive
from deploy.seed_release import install_active, install_generation


class SeedRound7Tests(unittest.TestCase):
    def test_active_is_create_only_and_exact_retry_only(self):
        with tempfile.TemporaryDirectory() as td:
            active = Path(td) / "active.json"
            descriptor = {"schema_version": 2, "sequence": 1, "generation": "seed"}
            install_active(active, descriptor)
            original = active.read_bytes()
            install_active(active, descriptor)
            self.assertEqual(active.read_bytes(), original)
            active.write_text(json.dumps({**descriptor, "sequence": 2}, sort_keys=True, separators=(",", ":")) + "\n")
            with self.assertRaisesRegex(SystemExit, "active descriptor mismatch"):
                install_active(active, descriptor)
            self.assertEqual(json.loads(active.read_text())["sequence"], 2)

    def test_generation_copy_is_fully_revalidated_before_rename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root / "src"; target = root / "releases" / "g1"; target.parent.mkdir()
            (src / "site").mkdir(parents=True); (src / "site/index.html").write_text("ok")
            (src / "release.json").write_text('{"generation":"g1","artifacts":{"site/index.html":"x"}}')
            descriptor = {"generation":"g1", "artifacts":{"site/index.html":"x"}}
            seen = []
            def verify(path):
                seen.append(Path(path))
                if len(seen) == 2:
                    raise SystemExit("staging signature invalid")
                return json.loads((Path(path) / "release.json").read_text())
            with self.assertRaisesRegex(SystemExit, "staging signature invalid"):
                install_generation(src, target, descriptor, verify_release=verify)
            self.assertEqual(seen[0], src)
            self.assertNotEqual(seen[1], src)
            self.assertFalse(target.exists())


class InventoryRound7Tests(unittest.TestCase):
    def test_inventory_deduplicates_exact_old_paths_and_rejects_overlap(self):
        self.assertEqual(normalize_inventory(["/opt/dek-web", "/opt/dek-web", "/etc/passwd"]), ["/opt/dek-web", "/etc/passwd"])
        with self.assertRaisesRegex(SystemExit, "overlapping"):
            normalize_inventory(["/var/lib/dek-web", "/var/lib/dek-web/secrets/key"])

    def test_real_gnu_tar_from_normalized_inventory_validates(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); tree=root/"tree"; (tree/"var/lib/dek-web/secrets").mkdir(parents=True)
            (tree/"var/lib/dek-web/secrets/key").write_text("x")
            approved=(tree/"var/lib/dek-web").as_posix()
            inventory=normalize_inventory([approved, approved])
            nul=root/"existing.nul"; nul.write_bytes(approved.encode()+b"\0")
            archive=root/"files.tar"
            subprocess.run(["tar","--null","--verbatim-files-from",f"--files-from={nul}","-cpf",str(archive)],check=True)
            validate_archive(archive,inventory)


class EnablementRound7Tests(unittest.TestCase):
    def test_restore_enablement_emits_exact_systemctl_argv(self):
        states={
            "a.service":"enabled", "b.service":"enabled-runtime", "c.service":"disabled",
            "d.service":"masked", "e.service":"masked-runtime", "f.service":"static",
            "g.service":"indirect", "j.service":"alias",
        }
        calls=[]
        restore_enablement(states, runner=lambda *argv: calls.append(argv))
        self.assertEqual(calls, [
            ("systemctl","unmask","a.service"),("systemctl","unmask","--runtime","a.service"),
            ("systemctl","disable","a.service"),("systemctl","disable","--runtime","a.service"),
            ("systemctl","enable","a.service"),
            ("systemctl","unmask","b.service"),("systemctl","unmask","--runtime","b.service"),
            ("systemctl","disable","b.service"),("systemctl","disable","--runtime","b.service"),
            ("systemctl","enable","--runtime","b.service"),
            ("systemctl","unmask","c.service"),("systemctl","unmask","--runtime","c.service"),
            ("systemctl","disable","c.service"),("systemctl","disable","--runtime","c.service"),
            ("systemctl","unmask","d.service"),("systemctl","unmask","--runtime","d.service"),
            ("systemctl","disable","d.service"),("systemctl","disable","--runtime","d.service"),
            ("systemctl","mask","--force","d.service"),
            ("systemctl","unmask","e.service"),("systemctl","unmask","--runtime","e.service"),
            ("systemctl","disable","e.service"),("systemctl","disable","--runtime","e.service"),
            ("systemctl","mask","--runtime","e.service"),
        ])


class RunbookRound7Tests(unittest.TestCase):
    def test_nginx_is_reloaded_and_verified_on_forward_and_rollback(self):
        text=Path("deploy/PRODUCTION_ROLLOUT.md").read_text()
        self.assertGreaterEqual(text.count("systemctl reload nginx"),2)
        self.assertGreaterEqual(text.count("systemctl is-active --quiet nginx"),2)


if __name__ == "__main__": unittest.main()
