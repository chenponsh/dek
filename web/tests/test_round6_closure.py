import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.activator import Activator, ActivatorConfig, fsync_tree
from deploy.seed_release import active_metadata, install_generation
from deploy.rollback import validate_archive, validate_preconditions
from web.review import decision_mac, iter_valid_decisions


class SeedReleaseRound6Tests(unittest.TestCase):
    def test_seed_descriptor_is_accepted_by_activator(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = ActivatorConfig.under(Path(td)); cfg.prepare()
            source = Activator(cfg, proof_reader=lambda *_: {}).test_release("seed", sequence=1)
            builder = json.loads((source / "release.json").read_text())
            builder.pop("sequence"); builder.pop("previous_generation")
            descriptor = active_metadata(builder)
            cfg.active.write_text(json.dumps(descriptor))
            self.assertEqual(Activator(cfg, proof_reader=lambda *_: {})._read_active(), descriptor)
            self.assertEqual(descriptor["sequence"], 1)
            self.assertIsNone(descriptor["previous_generation"])

    def test_seed_retry_recovers_existing_verified_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/"source"; releases=root/"releases"; releases.mkdir()
            (source/"site").mkdir(parents=True); (source/"site/index.html").write_text("ok")
            (source/"dek-kb.json").write_text("{}")
            meta={"generation":"seed","artifacts":{p:__import__('hashlib').sha256((source/p).read_bytes()).hexdigest() for p in ("site/index.html","dek-kb.json")}}
            (source/"release.json").write_text(json.dumps(meta))
            install_generation(source,releases/"seed",meta)
            install_generation(source,releases/"seed",meta)
            (releases/"seed/site/index.html").write_text("bad")
            with self.assertRaisesRegex(SystemExit,"existing generation"):
                install_generation(source,releases/"seed",meta)


class DurableReleaseRound6Tests(unittest.TestCase):
    def test_fsync_failure_prevents_generation_rename_and_active_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=ActivatorConfig.under(Path(td)); cfg.prepare()
            activator=Activator(cfg,proof_reader=lambda kind,expected:dict(expected))
            source=activator.test_release("g1",sequence=1)
            with patch("deploy.activator.fsync_tree",side_effect=OSError("disk")):
                with self.assertRaisesRegex(OSError,"disk"): activator.activate(source)
            self.assertFalse((cfg.releases/"g1").exists()); self.assertFalse(cfg.active.exists())

    def test_fsync_tree_files_before_nested_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"a/b").mkdir(parents=True); (root/"a/b/f").write_text("x")
            events=[]; real_open=os.open
            def opening(path,*args,**kwargs):
                events.append(("open",Path(path).relative_to(root).as_posix()))
                return real_open(path,*args,**kwargs)
            with patch("deploy.activator.os.open",side_effect=opening), patch("deploy.activator.os.fsync",side_effect=lambda fd: events.append(("fsync",fd))):
                fsync_tree(root)
            opens=[name for kind,name in events if kind=="open"]
            self.assertLess(opens.index("a/b/f"),opens.index("a/b")); self.assertLess(opens.index("a/b"),opens.index("a"))


class RollbackRound6Tests(unittest.TestCase):
    def _archive(self, root, members):
        archive=root/"files.tar"
        with tarfile.open(archive,"w") as out:
            for name,kind in members:
                info=tarfile.TarInfo(name)
                if kind=="file": info.size=1; out.addfile(info,io.BytesIO(b"x"))
                elif kind=="dir": info.type=tarfile.DIRTYPE; out.addfile(info)
                else: info.type=kind; info.linkname="etc/passwd"; out.addfile(info)
        return archive

    def test_archive_members_exactly_match_existing_and_reject_dangerous(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); archive=self._archive(root,[("etc/a","file")])
            validate_archive(archive,["/etc/a"])
            for members in ([("etc/a","file"),("etc/extra","file")],[("../etc/a","file")],[("/etc/a","file")],[("etc/a",tarfile.SYMTYPE)],[("etc/a","file"),("etc/a","file")]):
                archive=self._archive(root,members)
                with self.assertRaises(SystemExit): validate_archive(archive,["/etc/a"])

    def test_every_precondition_is_checked_before_first_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); proof=root/"proof"; proof.write_text("#!/bin/sh\n"); os.chmod(proof,0o700)
            (root/"unit-enablement.before").write_text("dek-web.service enabled\n")
            with patch("deploy.rollback.subprocess.run") as run:
                run.return_value.returncode=0
                validate_preconditions("dek-web.service","dek-qa.service",[str(proof)],root/"unit-enablement.before")
                self.assertTrue(any("cat" in call.args[0] for call in run.call_args_list))
            os.chmod(proof,0o600)
            with self.assertRaises(SystemExit): validate_preconditions("dek-web.service","dek-qa.service",[str(proof)],root/"unit-enablement.before")


class ImportAndQueueRound6Tests(unittest.TestCase):
    def test_isolated_direct_scripts_reach_help_without_import_error(self):
        env={"PATH":"/usr/bin:/bin"}
        for script in ("deploy/seed_release.py","deploy/source_ingest_entrypoint.py"):
            result=subprocess.run(["/usr/bin/env","-i","PATH=/usr/bin:/bin","/usr/bin/python3","-I",str(Path(script).resolve()),"--help"],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn("ModuleNotFoundError",result.stderr)

    def test_corrupt_queue_is_persistently_quarantined_and_valid_later_line_survives(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); q=root/"decisions.jsonl"; quarantine=root/"quarantine"; key=b"k"*32
            good={"record_type":"decision","decision_id":"good","action":"approve"}; good["decision_mac"]=decision_mac(good,key)
            q.write_bytes(b"{bad}\n"+(json.dumps(good)+"\n").encode())
            alerts=[]
            records=list(iter_valid_decisions(q,key,quarantine_dir=quarantine,on_corrupt=alerts.append))
            self.assertEqual([r["decision_id"] for r in records],["good"])
            self.assertEqual(len(list(quarantine.glob("*.json"))),1); self.assertEqual(len(alerts),1)


class RunbookRound6Tests(unittest.TestCase):
    def test_profile_and_backup_contracts_are_documented(self):
        text=Path("deploy/PRODUCTION_ROLLOUT.md").read_text()
        self.assertIn("/var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml",text)
        self.assertTrue(any(line.startswith("sha256sum ") and "unit-enablement.before" in line for line in text.splitlines()))
        self.assertIn("--sha256sums",text)


if __name__ == "__main__": unittest.main()
