import hashlib
import io
import json
import os
import re
import base64
import tempfile
import unittest
import shutil
import subprocess
import threading
import fcntl
from contextlib import redirect_stderr
from pathlib import Path, PurePosixPath
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from deploy.activator import ActivationError, Activator, ActivatorConfig
from deploy.release_bundle import BundleBuilder, BundleError, ReleasePublisher, _canonical_release
from deploy.readiness import ReadinessError, validate_configuration
from qa.dek_qa.index import build_index
from qa.dek_qa.mcp_server import ActiveIndex
from web.app import ActiveSite, KnowledgeApp
from web.auth import sign_claim
from web.tests.test_app import FakeGateway
from web.review import IsolatedReviewClone, MemoryFormNonceStore
from deploy.builder_entrypoint import build_atomically, main as builder_main
from deploy.publisher_entrypoint import process_decision
from deploy.publisher_entrypoint import process_records


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(".new")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


class LiveGenerationSliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.releases.mkdir()
        self.active = self.root / "active.json"
        self.secret = b"0123456789abcdef0123456789abcdef"
        self.token = sign_claim({"user_id":"u1", "display_name":"u", "kbot_allowed":True, "exp":2000000000}, self.secret)

    def tearDown(self): self.temp.cleanup()

    def release(self, name: str, text: str) -> dict:
        release = self.releases / name
        (release / "site/wiki").mkdir(parents=True)
        (release / "site/wiki/a.html").write_text(text, encoding="utf-8")
        (release / "site/index.html").write_text(text, encoding="utf-8")
        vault = self.root / ("vault-" + name)
        (vault / "wiki").mkdir(parents=True)
        (vault / "wiki/a.md").write_text(f"---\nquestion: {text}\n---\n\n{text}\n", encoding="utf-8")
        build_index(vault, release / "dek-kb.json")
        index_digest = hashlib.sha256((release / "dek-kb.json").read_bytes()).hexdigest()
        web_digest = hashlib.sha256((release / "site/index.html").read_bytes()).hexdigest()
        metadata = {"schema_version":2, "sequence":1 if name == "g1" else 2,
                    "nonce":"nonce-" + name + "-12345678", "generation":name,
                    "previous_generation":None if name == "g1" else "g1",
                    "commit":"1"*40, "tree":"2"*40, "bundle_sha256":"3"*64,
                    "artifacts":{"dek-kb.json":index_digest, "site/index.html":web_digest}}
        (release / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        (release / "release.lock").touch()
        return metadata

    def test_web_reads_active_once_per_request_and_switches_without_restart(self):
        one = self.release("g1", "one"); two = self.release("g2", "two")
        atomic_json(self.active, one)
        app = KnowledgeApp(ActiveSite(self.active, self.releases), FakeGateway(), self.secret, clock=lambda:1900000000)
        def call():
            status=[]
            body=b"".join(app({"PATH_INFO":"/wiki/a.html", "QUERY_STRING":"", "HTTP_COOKIE":"dek_session="+self.token,
                               "wsgi.input":io.BytesIO(b""), "CONTENT_LENGTH":"0"}, lambda s,h:status.append(s)))
            return status[0], body
        self.assertEqual(call(), ("200 OK", b"one"))
        atomic_json(self.active, two)
        self.assertEqual(call(), ("200 OK", b"two"))

    def test_cleanup_skips_release_pinned_by_inflight_web_request(self):
        one=self.release("g1","one"); atomic_json(self.active,one)
        pinned=ActiveSite(self.active,self.releases).pin()
        self.assertTrue(hasattr(pinned,"close"))
        os.utime(self.releases/"g1",(1,1))
        newer=self.releases/"newer"; newer.mkdir(); (newer/"release.lock").touch()
        future=dict(one); future["generation"]="g9"; future["previous_generation"]="g8"; future["nonce"]="nonce-g9-12345678"
        atomic_json(self.active,future)
        config=ActivatorConfig(build_inbox=self.releases,releases=self.releases,control=self.active.parent,journal=self.active.parent,outcomes=self.active.parent,spent=self.active.parent,active=self.active,retain=1)
        Activator(config,proof_reader=lambda k,e:dict(e)).cleanup()
        self.assertTrue((self.releases/"g1").exists())
        pinned.close()

    def test_qa_validates_complete_index_then_atomically_swaps_or_keeps_last_good(self):
        one = self.release("g1", "one"); two = self.release("g2", "two")
        atomic_json(self.active, one)
        live = ActiveIndex(self.active, self.releases)
        self.assertTrue(live.current().dek_kb_search("one"))
        atomic_json(self.active, two)
        self.assertTrue(live.refresh())
        self.assertTrue(live.current().dek_kb_search("two"))
        (self.releases / "g2/dek-kb.json").write_text("{}", encoding="utf-8")
        self.assertFalse(live.refresh())
        self.assertTrue(live.current().dek_kb_search("two"))

    def test_qa_concurrent_refresh_writes_one_valid_atomic_proof(self):
        one=self.release("g1","one"); atomic_json(self.active,one); proof=self.root/"proof.json"
        live=ActiveIndex(self.active,self.releases,proof); errors=[]
        def refresh_many():
            try:
                for _ in range(50): self.assertTrue(live.refresh())
            except Exception as exc: errors.append(exc)
        workers=[threading.Thread(target=refresh_many) for _ in range(8)]
        [item.start() for item in workers]; [item.join() for item in workers]
        self.assertEqual(errors,[]); self.assertEqual(json.loads(proof.read_text())["generation"],"g1")


class BundleBoundarySliceTests(unittest.TestCase):
    def test_activator_revalidates_complete_copied_target_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=ActivatorConfig.under(Path(temporary)); config.prepare()
            activator=Activator(config,proof_reader=lambda kind,expected:dict(expected))
            source=activator.test_release("copy-race",sequence=1)
            original=shutil.copytree
            def poisoned(*args,**kwargs):
                result=original(*args,**kwargs)
                if len(args)==2 and isinstance(args[0],Path):
                    (Path(args[1])/"site/run.py").write_text("bad")
                return result
            from unittest.mock import patch
            with patch("deploy.activator.shutil.copytree",side_effect=poisoned):
                with self.assertRaisesRegex(ActivationError,"executable content"):
                    activator.activate(source)
            self.assertFalse(config.active.exists())

    def test_final_generation_and_complete_artifacts_are_publisher_signed(self):
        key=Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary)
            approval={"schema_version":2,"decision_id":"decision-12345678","decision_sha256":"1"*64,"nonce":"decision-12345678","origin":"https://github.com/chenponsh/dek.git","commit":"2"*40,"tree":"3"*40,"bundle_sha256":"4"*64}
            (package/"approval.json").write_text(json.dumps(approval))
            (package/"approval.sig").write_bytes(key.sign(b"dek-approved-bundle-v2\0"+json.dumps(approval,sort_keys=True,separators=(",",":")).encode()))
            (package/"site").mkdir(); (package/"site/index.html").write_text("ok"); (package/"dek-kb.json").write_text('{"version":4,"documents":[]}')
            artifacts={"dek-kb.json":hashlib.sha256((package/"dek-kb.json").read_bytes()).hexdigest(),"site/index.html":hashlib.sha256((package/"site/index.html").read_bytes()).hexdigest()}
            claim={**approval,"generation":"decision-12345678-decision-12345678","artifacts":artifacts}
            (package/"release.json").write_text(json.dumps(claim))
            publisher=object.__new__(ReleasePublisher); publisher.signing_key=key
            final=publisher.finalize(package)
            self.assertEqual(final["generation"],"decision-12345678-decision-12345678")
            self.assertEqual(final["artifacts"],artifacts)
            key.public_key().verify((package/"release.sig").read_bytes(), _canonical_release(final))

    def test_publish_refuses_to_push_before_fixed_build_gate(self):
        key=Ed25519PrivateKey.generate(); publisher=object.__new__(ReleasePublisher); publisher.signing_key=key; publisher.origin="https://github.com/chenponsh/dek.git"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(BundleError,"final release"):
                publisher.publish(Path(temporary))

    def test_review_snapshot_bundle_bytes_and_objects_are_reverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/"repo"; repo.mkdir(); subprocess.run(["git","init","-q"],cwd=repo,check=True)
            (repo/"a").write_text("x"); subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","x"],cwd=repo,check=True)
            commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip(); tree=subprocess.check_output(["git","rev-parse","HEAD^{tree}"],cwd=repo,text=True).strip()
            bundle=root/"snapshot.bundle"; subprocess.run(["git","bundle","create",str(bundle),"HEAD"],cwd=repo,check=True)
            digest=hashlib.sha256(bundle.read_bytes()).hexdigest()
            ReleasePublisher.verify_review_snapshot(bundle,commit,tree,digest)
            with self.assertRaisesRegex(BundleError,"snapshot bundle digest"):
                ReleasePublisher.verify_review_snapshot(bundle,commit,tree,"0"*64)

    def test_reviewer_archives_original_snapshot_bundle_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/"repo"; repo.mkdir(); subprocess.run(["git","init","-q","-b","main"],cwd=repo,check=True)
            (repo/"a").write_text("x"); subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","x"],cwd=repo,check=True)
            bundle=root/"repository.bundle"; subprocess.run(["git","bundle","create",str(bundle),"refs/heads/main"],cwd=repo,check=True)
            archive=root/"archive"
            source=IsolatedReviewClone(bundle,root/"clones",bundle_archive=archive)
            source.current()
            digest=hashlib.sha256(bundle.read_bytes()).hexdigest()
            self.assertEqual((archive/(digest+".bundle")).read_bytes(),bundle.read_bytes())

    def test_publish_pipeline_gates_on_build_and_retries_idempotently(self):
        calls=[]
        class MockPublisher:
            origin="https://github.com/chenponsh/dek.git"
            @staticmethod
            def verify_review_snapshot(bundle,commit,tree,digest): calls.append(("verify",bundle.name))
            def prepare_change(self,output,decision):
                output.mkdir(parents=True,exist_ok=False)
                (output/"approval.json").write_text(json.dumps({"decision_id":decision["decision_id"],"nonce":decision["decision_id"]}))
                calls.append(("prepare",decision["decision_id"]))
            def finalize(self,package): (package/"release.sig").write_text("sig"); calls.append(("finalize",package.name))
            def publish(self,package): calls.append(("publish",package.name))
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); approved=root/"approved"; approved.mkdir(); builds=root/"builds"; builds.mkdir(); archive=root/"archive"; archive.mkdir()
            archive_bundle=archive/"deadbeef.bundle"; archive_bundle.write_bytes(b"bundle")
            decision={"decision_id":"decision-12345678","snapshot_commit":"1"*40,"snapshot_tree":"2"*40,"snapshot_bundle_sha256":"deadbeef"}
            pub=MockPublisher()
            self.assertEqual(process_decision(pub,decision,approved,builds,archive),"wait")
            build=builds/"decision-12345678-decision-12345678"; build.mkdir(); (build/"release.json").write_text("{}")
            self.assertEqual(process_decision(pub,decision,approved,builds,archive),"pushed")
            self.assertEqual(process_decision(pub,decision,approved,builds,archive),"pushed")
            self.assertEqual(calls.count(("finalize","decision-12345678-decision-12345678")),1)
            self.assertEqual(calls.count(("publish","decision-12345678-decision-12345678")),2)
            self.assertIn(("verify","deadbeef.bundle"),calls)
    def test_builder_crash_never_leaves_complete_target_and_retry_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); package=root/"package"; package.mkdir(); builds=root/"builds"; builds.mkdir(); target=builds/"release"
            class Builder:
                def __init__(self): self.fail=True
                def build(self,_package,output):
                    output.mkdir(); (output/"partial").write_text("x")
                    if self.fail: raise RuntimeError("crash")
                    (output/"release.json").write_text("{}")
            builder=Builder()
            with self.assertRaisesRegex(RuntimeError,"crash"): build_atomically(builder,package,target)
            self.assertFalse(target.exists())
            builder.fail=False; build_atomically(builder,package,target)
            self.assertTrue((target/"release.json").is_file()); self.assertFalse((builds/(".staging-"+target.name)).exists())
    def test_publisher_builder_use_signed_bundle_and_private_snapshot(self):
        self.assertTrue(callable(ReleasePublisher))
        self.assertTrue(callable(BundleBuilder))
        code = Path("deploy/release_bundle.py").read_text(encoding="utf-8")
        self.assertIn("git bundle", code)
        self.assertIn("private_snapshot", code)
        self.assertNotIn("shell=True", code)
        self.assertNotIn("worktree", code.lower())

    def test_builder_rejects_server_side_executable_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "site").mkdir()
            (output / "site/index.html").write_text("ok")
            (output / "site/run.py").write_text("bad")
            (output / "dek-kb.json").write_text("{}")
            with self.assertRaises(BundleError):
                BundleBuilder.validate_static_output(output)

    def test_builder_verifies_bundle_objects_and_runs_only_from_extracted_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/"repo"; repo.mkdir()
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            (repo/"wiki").mkdir(); (repo/"wiki/a.md").write_text("---\nquestion: q\n---\n\na\n")
            subprocess.run(["git","add","."],cwd=repo,check=True)
            subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","one"],cwd=repo,check=True)
            commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip()
            tree=subprocess.check_output(["git","rev-parse","HEAD^{tree}"],cwd=repo,text=True).strip()
            package=root/"package"; package.mkdir(); bundle=package/"repository.bundle"
            subprocess.run(["git","bundle","create",str(bundle),"HEAD"],cwd=repo,check=True)
            approval={"schema_version":2,"decision_id":"decision-12345678","nonce":"nonce-12345678","origin":"https://github.com/chenponsh/dek.git",
                      "commit":commit,"tree":tree,"bundle_sha256":hashlib.sha256(bundle.read_bytes()).hexdigest()}
            canonical=b"dek-approved-bundle-v2\0"+json.dumps(approval,sort_keys=True,separators=(",",":")).encode()
            key=Ed25519PrivateKey.generate(); (package/"approval.json").write_text(json.dumps(approval)); (package/"approval.sig").write_bytes(key.sign(canonical))
            observed=[]
            def runner(command,*,cwd=None,**unused):
                observed.append((command,Path(cwd)))
                if "web.site" in command:
                    output=Path(command[command.index("--output")+1]); output.mkdir(parents=True); (output/"index.html").write_text("ok")
                if "qa.dek_qa.build_index" in command:
                    output=Path(command[command.index("--output")+1]); output.write_text('{"version":4,"documents":[]}')
                return b""
            output=root/"output"; BundleBuilder(key.public_key(),runner=runner).build(package,output)
            self.assertTrue(observed)
            self.assertTrue(all(path.name=="snapshot" for _,path in observed))
            self.assertFalse(any(path==repo for _,path in observed))
            self.assertTrue((output/"approval.sig").is_file())

    def test_reviewer_refreshes_its_own_clone_from_atomic_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/"repo"; repo.mkdir(); bundle=root/"repository.bundle"
            subprocess.run(["git","init","-q","-b","main"],cwd=repo,check=True)
            (repo/"ingestion/rough").mkdir(parents=True); note=repo/"ingestion/rough/a.md"; note.write_text("one")
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","one"],cwd=repo,check=True)
            subprocess.run(["git","bundle","create",str(bundle),"refs/heads/main"],cwd=repo,check=True)
            source=IsolatedReviewClone(bundle,root/"review-clones")
            first=source.current(); self.assertEqual((first/"ingestion/rough/a.md").read_text(),"one"); self.assertTrue((first/".git").is_dir())
            note.write_text("two"); subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","two"],cwd=repo,check=True)
            replacement=root/"new.bundle"; subprocess.run(["git","bundle","create",str(replacement),"refs/heads/main"],cwd=repo,check=True); os.replace(replacement,bundle)
            second=source.current(); self.assertNotEqual(first,second); self.assertEqual((second/"ingestion/rough/a.md").read_text(),"two")


class ActivationSliceTests(unittest.TestCase):
    def test_reconcile_multiple_prepared_journals_rereads_active_and_freshly_proves_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=ActivatorConfig.under(Path(temporary)); config.prepare(); calls=[]
            seed=Activator(config,proof_reader=lambda k,e:dict(e)); g1=seed.test_release("g1",sequence=1); seed.activate(g1)
            g2=seed.test_release("g2",sequence=2); m2=seed._validate_release(g2); previous=json.loads(config.active.read_text())
            atomic_json(config.active,m2)
            atomic_json(config.journal/"000-a.json",{"status":"prepared","requested":m2,"previous":previous,"recorded_at":1})
            atomic_json(config.journal/"001-b.json",{"status":"prepared","requested":previous,"previous":None,"recorded_at":2})
            def proof(kind,expected):
                calls.append((kind,expected["generation"]))
                if expected["generation"]=="g2": raise ActivationError("fail new")
                return dict(expected)
            Activator(config,proof_reader=proof).reconcile()
            self.assertEqual(json.loads(config.active.read_text())["generation"],"g1")
            self.assertGreaterEqual(calls.count(("web","g1")),1)
            self.assertGreaterEqual(calls.count(("qa","g1")),1)

    def test_journal_precedes_atomic_switch_and_fresh_proofs_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ActivatorConfig.under(root)
            config.prepare()
            calls=[]
            activator = Activator(config, proof_reader=lambda kind, expected: calls.append((kind, expected["nonce"])) or dict(expected))
            first = activator.test_release("g1", sequence=1)
            activator.activate(first)
            self.assertEqual(json.loads(config.active.read_text())["generation"], "g1")
            self.assertEqual([item[0] for item in calls], ["web", "qa"])
            journals = list(config.journal.glob("*.json"))
            self.assertTrue(journals)
            self.assertEqual(json.loads(journals[0].read_text())["status"], "succeeded")

    def test_unknown_third_generation_stops_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = ActivatorConfig.under(root); config.prepare()
            activator = Activator(config, proof_reader=lambda kind, expected: dict(expected))
            activator.activate(activator.test_release("g1", sequence=1))
            bad = activator.test_release("g2", sequence=2)
            def proof(kind, expected):
                if expected["generation"] == "g2":
                    atomic_json(config.active, {**expected, "generation":"g3", "nonce":"nonce-g3-12345678"})
                    raise ActivationError("probe failed")
                return dict(expected)
            activator.proof_reader = proof
            with self.assertRaisesRegex(ActivationError, "unknown third generation"):
                activator.activate(bad)
            self.assertEqual(json.loads(config.active.read_text())["generation"], "g3")

    def test_hostile_symlink_and_spent_nonce_replay_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=ActivatorConfig.under(Path(temporary)); config.prepare()
            activator=Activator(config,proof_reader=lambda kind,expected:dict(expected))
            release=activator.test_release("good",sequence=1); activator.activate(release)
            with self.assertRaisesRegex(ActivationError,"spent nonce"): activator.activate(release)
            hostile=activator.test_release("hostile",sequence=2)
            (hostile/"dek-kb.json").unlink(); (hostile/"dek-kb.json").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ActivationError,"unsafe entry"): activator.activate(hostile)

    def test_failed_new_proofs_restore_and_freshly_prove_old_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=ActivatorConfig.under(Path(temporary)); config.prepare(); calls=[]
            def proof(kind,expected):
                calls.append((kind,expected["generation"]))
                if expected["generation"]=="g2": raise ActivationError("new failed")
                return dict(expected)
            activator=Activator(config,proof_reader=proof)
            activator.activate(activator.test_release("g1",sequence=1)); calls.clear()
            with self.assertRaisesRegex(ActivationError,"old generation restored"): activator.activate(activator.test_release("g2",sequence=2))
            self.assertEqual(json.loads(config.active.read_text())["generation"],"g1")
            self.assertIn(("web","g1"),calls); self.assertIn(("qa","g1"),calls)


class DeploymentContractTests(unittest.TestCase):
    def test_obsolete_privileged_candidates_are_absent(self):
        for path in ("deploy/stage_a.py", "deploy/stage_a_inventory.py", "deploy/deployment_controller.py",
                     "deploy/deployment-controller.json", "deploy/systemd/dek-deployment-controller.service",
                     "deploy/systemd/dek-deployment-controller.timer", "deploy/systemd/dek-generation-boot.service",
                     "deploy/review_gate.py", "deploy/gate_entrypoint.py", "deploy/git_wrapper.py"):
            self.assertFalse(Path(path).exists(), path)

    def test_all_stage_b_services_are_non_root_capability_empty_and_controller_free(self):
        # *-manual.service units are a deliberately different, narrow category:
        # tiny root-owned bridges that only exist to turn an unprivileged
        # reviewer-written marker file into `systemctl start` of one fixed,
        # named unit (see dek-source-ingest-manual.service, already live, and
        # dek-review-publish-manual.service) -- never a general admin surface.
        manual_bridges = set(Path("deploy/systemd").glob("dek-*-manual.service"))
        for path in Path("deploy/systemd").glob("dek-*.service"):
            if path in manual_bridges: continue
            text = path.read_text(encoding="utf-8")
            if "@" in path.name: continue
            self.assertRegex(text, r"(?m)^User=dek-[a-z-]+$")
            self.assertIn("CapabilityBoundingSet=", text)
            self.assertIn("AmbientCapabilities=", text)
            for forbidden in ("User=root", "sudo", "systemctl", "org.freedesktop.systemd", "/run/systemd/private"):
                self.assertNotIn(forbidden, text)
        for path in manual_bridges:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("User=root", text)
            self.assertIn("NoNewPrivileges=true", text)
            self.assertRegex(text, r"ExecStart=/usr/bin/systemctl start (?:--wait )?dek-[a-z0-9@.-]+\n")
        for name in ("dek-review-publish.service","dek-source-ingest.service","dek-builder.service","dek-activator.service"):
            unit=Path("deploy/systemd",name).read_text()
            self.assertNotIn("ConditionPathExists",unit)
            self.assertIn("ExecCondition=+",unit)
            self.assertIn("--validate-marker --marker /var/lib/dek-readiness/automation-ready",unit)

    def test_manual_runbook_has_exact_install_backup_and_rollback_inventory(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("independently verified package", text)
        self.assertIn("Checkpoint A0", text)
        self.assertIn("install -o root -g root -m 0644", text)
        self.assertIn("backup-inventory.txt", text)
        self.assertIn("rollback-inventory.txt", text)
        self.assertNotIn("stage_a.py", text)

    def test_setgid_dac_matrix_and_fixed_git_credential_are_declared(self):
        matrix = Path("deploy/DAC_MATRIX.tsv").read_text(encoding="utf-8")
        for flow in ("decision-to-publisher", "build-to-activator", "activator-to-web-qa", "outcome-to-publisher"):
            self.assertIn(flow, matrix)
        publisher = Path("deploy/systemd/dek-review-publish.service").read_text(encoding="utf-8")
        self.assertIn("LoadCredential=git-credentials:", publisher)
        self.assertIn("DEK_FIXED_ORIGIN=https://github.com/chenponsh/dek.git", publisher)
        self.assertNotIn("dek-repository", publisher)
        self.assertNotIn("dek-repository", Path("deploy/systemd/dek-source-ingest.service").read_text())

    def test_review_readiness_fails_closed_on_ids_tls_and_callback(self):
        valid={"review_origin":"https://review.example", "oauth_callback":"https://review.example/auth/callback",
               "reviewer_ids":["enterprise-123"], "expected_addresses":["192.0.2.10"],
               "readiness_url":"https://review.example/__ready"}
        self.assertEqual(validate_configuration(valid),("review.example",443))
        for change in ({"reviewer_ids":[]},{"review_origin":"http://review.example"},{"oauth_callback":"https://other.example/auth/callback"}):
            with self.assertRaises(ReadinessError): validate_configuration({**valid,**change})

    def test_systemd_credential_validation_uses_open_inode_and_exact_mode(self):
        code=Path("deploy/release_bundle.py").read_text(encoding="utf-8")
        self.assertIn("os.fstat",code)
        self.assertIn("O_NOFOLLOW",code)
        self.assertIn("0o400",code)

    @unittest.skipUnless(os.geteuid()==0 and shutil.which("setpriv"),"requires root and setpriv")
    def test_real_numeric_uid_gid_dac_positive_negative_and_secret_probes(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root=Path(temporary); os.chmod(root,0o755)
            producer,consumer,unrelated,shared=62001,62002,62003,62020
            for flow in ("decision","build","release","outcome"):
                directory=root/flow; directory.mkdir(); os.chown(directory,producer,shared); os.chmod(directory,0o2750)
                artifact=directory/"artifact"; artifact.write_text(flow); os.chown(artifact,producer,shared); os.chmod(artifact,0o640)
                allowed=subprocess.run(["setpriv",f"--reuid={consumer}",f"--regid={consumer}",f"--groups={shared}","test","-r",str(artifact)])
                denied=subprocess.run(["setpriv",f"--reuid={unrelated}",f"--regid={unrelated}","--clear-groups","test","-r",str(artifact)])
                denied_write=subprocess.run(["setpriv",f"--reuid={consumer}",f"--regid={consumer}",f"--groups={shared}","test","-w",str(artifact)])
                self.assertEqual(allowed.returncode,0,flow); self.assertNotEqual(denied.returncode,0,flow); self.assertNotEqual(denied_write.returncode,0,flow)
            secrets=root/"secrets"; secrets.mkdir(); os.chown(secrets,producer,producer); os.chmod(secrets,0o700)
            credential=secrets/"credential"; credential.write_text("not-a-real-secret"); os.chown(credential,producer,producer); os.chmod(credential,0o400)
            command=["setpriv",f"--reuid={producer}",f"--regid={producer}","--clear-groups","/usr/bin/python3","-c",
                     "from pathlib import Path; from deploy.release_bundle import validate_systemd_credential; validate_systemd_credential(Path("+repr(str(credential))+"))"]
            self.assertEqual(subprocess.run(command,cwd=Path.cwd()).returncode,0)
            self.assertNotEqual(subprocess.run(["setpriv",f"--reuid={consumer}",f"--regid={consumer}","--clear-groups","test","-r",str(credential)]).returncode,0)


class Round3SliceTests(unittest.TestCase):
    def test_auth_env_returns_real_basic_header_and_source_has_no_placeholder(self):
        from deploy.release_bundle import _auth_env
        with tempfile.TemporaryDirectory() as temporary:
            credential = Path(temporary) / "git-credentials"
            credential.write_text("https://alice:s3cret@github.com/chenponsh/dek.git", encoding="utf-8")
            env = _auth_env("https://github.com/chenponsh/dek.git", credential)
            self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/chenponsh/dek.git.extraHeader")
            expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], "Authorization: " + expected)
        for rel in ("deploy/release_bundle.py", "deploy/source_ingest_entrypoint.py"):
            self.assertNotIn("***", Path(rel).read_text(encoding="utf-8"), rel)

    def test_builder_and_publisher_agree_on_generation_directory_end_to_end(self):
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"; repo.mkdir()
            (repo / "web" / "assets").mkdir(parents=True)
            shutil.copy2("web/site.py", repo / "web" / "site.py")
            for f in ("style.css", "app.js", "search.js"):
                shutil.copy2(f"web/assets/{f}", repo / f"web/assets/{f}")
            (repo / "qa" / "dek_qa").mkdir(parents=True)
            for f in ("__init__.py", "index.py", "build_index.py"):
                shutil.copy2(f"qa/dek_qa/{f}", repo / f"qa/dek_qa/{f}")
            (repo / "ingestion" / "automation").mkdir(parents=True)
            for f in ("__init__.py", "audit.py", "audit_exclusions.json"):
                shutil.copy2(f"ingestion/automation/{f}", repo / f"ingestion/automation/{f}")
            for d in ("ingestion/automation/tests", "web/tests", "qa/tests"):
                (repo / d).mkdir(parents=True)
                (repo / d / "test_trivial.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n", encoding="utf-8")
            (repo / "wiki").mkdir()
            (repo / "wiki" / "a.md").write_text("---\nquestion: q\n---\n\nbody\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@i", "commit", "-qm", "one"], cwd=repo, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
            bundle = root / "repository.bundle"
            subprocess.run(["git", "bundle", "create", str(bundle), "HEAD"], cwd=repo, check=True)
            bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
            decision_id = "decision-12345678"; nonce = decision_id
            approval = {"schema_version": 2, "decision_id": decision_id, "nonce": nonce,
                        "origin": "https://github.com/chenponsh/dek.git", "commit": commit, "tree": tree, "bundle_sha256": bundle_sha}
            canonical = b"dek-approved-bundle-v2\0" + json.dumps(approval, sort_keys=True, separators=(",", ":")).encode()
            approved = root / "approved"; builds = root / "builds"; approved.mkdir(); builds.mkdir()
            pkg = approved / decision_id; pkg.mkdir()
            (pkg / "approval.json").write_text(json.dumps(approval), encoding="utf-8")
            (pkg / "approval.sig").write_bytes(key.sign(canonical))
            shutil.copy2(bundle, pkg / "repository.bundle")
            pub = root / "pub.pem"
            pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            cfg = root / "builder.json"
            cfg.write_text(json.dumps({"approved_root": str(approved), "build_root": str(builds), "approval_public_key": str(pub)}), encoding="utf-8")
            self.assertEqual(builder_main(["--config", str(cfg)]), 0)
            generation = f"{decision_id}-{nonce}"
            self.assertTrue((builds / generation / "release.json").is_file(), "builder must emit the generation-named directory")
            self.assertFalse((builds / decision_id).exists(), "builder must not emit a raw decision-id directory")
            archive = root / "archive"; archive.mkdir()
            shutil.copy2(bundle, archive / (bundle_sha + ".bundle"))
            decision = {"decision_id": decision_id, "snapshot_commit": commit, "snapshot_tree": tree, "snapshot_bundle_sha256": bundle_sha}
            publisher = ReleasePublisher("https://github.com/chenponsh/dek.git", key, root / "missing-credential")
            try:
                process_decision(publisher, decision, approved, builds, archive)
            except (BundleError, FileNotFoundError, OSError):
                pass
            self.assertTrue((builds / generation / "release.sig").is_file(), "publisher must locate the generation directory and finalize")

    def test_activator_is_read_only_on_builds_dac_declaration(self):
        sysusers = Path("deploy/sysusers/dek-review-deploy.conf").read_text(encoding="utf-8")
        self.assertNotRegex(sysusers, re.compile(r"^m\s+dek-activator\s+dek-build-(share|write)\s*$", re.M))
        self.assertRegex(sysusers, re.compile(r"^m\s+dek-publisher\s+dek-build-write\s*$", re.M))
        tmpfiles = Path("deploy/tmpfiles/dek-review-deploy.conf").read_text(encoding="utf-8")
        m = re.search(r"^d\s+/var/spool/dek-activate/builds\s+(\d+)\s+(\S+)\s+(\S+)\s", tmpfiles, re.M)
        self.assertIsNotNone(m, "builds dir must be declared in tmpfiles")
        mode, owner, group = int(m.group(1), 8), m.group(2), m.group(3)
        self.assertEqual(owner, "dek-builder")
        self.assertEqual(group, "dek-build-write")
        self.assertEqual(mode & 0o002, 0, "no world write on builds")
        self.assertEqual(mode & 0o020, 0o020, "group write for publisher")
        matrix = Path("deploy/DAC_MATRIX.tsv").read_text(encoding="utf-8")
        self.assertRegex(matrix, r"publisher-to-builder\tdek-publisher\tdek-builder\t/var/spool/dek-build/approved\t")
        self.assertNotIn("dek-build-share", matrix)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "requires root and setpriv")
    def test_real_probe_activator_cannot_write_builds_but_publisher_can(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary); os.chmod(root, 0o755)
            builds = root / "builds"; builds.mkdir()
            builder_uid, publisher_uid, activator_uid, write_gid = 63101, 63102, 63103, 63110
            os.chown(builds, builder_uid, write_gid); os.chmod(builds, 0o2775)
            pub_write = subprocess.run(["setpriv", f"--reuid={publisher_uid}", f"--regid={publisher_uid}", f"--groups={write_gid}", "test", "-w", str(builds)])
            self.assertEqual(pub_write.returncode, 0, "publisher must write builds")
            act_write = subprocess.run(["setpriv", f"--reuid={activator_uid}", f"--regid={activator_uid}", "--clear-groups", "test", "-w", str(builds)])
            self.assertNotEqual(act_write.returncode, 0, "activator must not write builds")
            act_read = subprocess.run(["setpriv", f"--reuid={activator_uid}", f"--regid={activator_uid}", "--clear-groups", "test", "-r", str(builds)])
            self.assertEqual(act_read.returncode, 0, "activator must read builds")

    def test_package_sha256_covers_complete_install_manifest(self):
        manifest = {}
        for line in Path("deploy/PACKAGE.sha256").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line: continue
            digest, rel = line.split(None, 1)
            manifest[rel] = digest
        expected = []
        for top in ("deploy", "web", "qa", "ingestion/automation"):
            for path in sorted(Path(top).rglob("*")):
                if path.is_dir(): continue
                rel = path.as_posix()
                if rel == "deploy/PACKAGE.sha256": continue
                if "__pycache__" in path.parts or path.suffix == ".pyc": continue
                expected.append(rel)
        self.assertEqual(sorted(set(expected) - set(manifest)), [], "PACKAGE.sha256 is missing install-manifest files")
        for rel, digest in manifest.items():
            self.assertEqual(hashlib.sha256(Path(rel).read_bytes()).hexdigest(), digest, rel)

    def test_fixed_command_sandbox_path_can_locate_node_and_uv(self):
        # BundleBuilder.FIXED_COMMANDS runs `web/tests` (needs `node`, for
        # test_search.py's real JS execution) and `qa/tests` (needs `uv`, for
        # test_dependency_lock.py) as a release gate via _run()'s safe_env.
        # PATH="/usr/bin:/bin" doesn't cover either -- every build silently
        # failed those steps regardless of what was actually being released.
        from deploy.release_bundle import _run
        try:
            output = _run(("/bin/sh", "-c", "command -v node && command -v uv")).decode()
        except Exception as exc:
            import os as _os
            diag = _run(("/bin/sh", "-c", "echo PATH=$PATH; ls -la /opt/dek-vendor/bin 2>&1; /opt/dek-vendor/bin/uv --version 2>&1; echo uv_rc=$?")).decode() if True else ""
            raise AssertionError(f"orig={exc!r} cwd={_os.getcwd()!r} diag={diag!r}") from exc
        self.assertIn("node", output)
        self.assertIn("uv", output)

    def test_runbook_documents_seed_lock_non_ff_and_builder_sandbox(self):
        text = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("release.lock", text)
        self.assertRegex(text.lower(), r"non-fast-forward|not a fast-forward|fast-forward.*reject")
        self.assertRegex(text.lower(), r"threat model|sandbox.*(build|test)|runs?.*(build|test).*code|executes.*(build|test)")


if __name__ == "__main__": unittest.main()


class Round4SliceTests(unittest.TestCase):
    def test_qa_proof_parent_is_traversable_but_not_writable_and_real_dac_works(self):
        tmpfiles=Path("deploy/tmpfiles/dek-review-deploy.conf").read_text()
        self.assertRegex(tmpfiles,r"(?m)^d /run/dek-proofs 0751 dek-activator dek-proof-read -$")
        self.assertRegex(tmpfiles,r"(?m)^d /run/dek-proofs/qa 2750 dek-qa dek-proof-read -$")
        if os.geteuid()!=0 or not shutil.which("setpriv"): return
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root=Path(temporary); os.chmod(root,0o755); parent=root/"proofs"; qa=parent/"qa"
            qa_uid,act_uid,bad_uid,proof_gid=64101,64102,64103,64110
            parent.mkdir(); os.chown(parent,act_uid,proof_gid); os.chmod(parent,0o751)
            qa.mkdir(); os.chown(qa,qa_uid,proof_gid); os.chmod(qa,0o2750)
            run=lambda uid,groups,*cmd: subprocess.run(["setpriv",f"--reuid={uid}",f"--regid={uid}",f"--groups={groups}" if groups else "--clear-groups",*cmd])
            self.assertEqual(run(qa_uid,proof_gid,"test","-x",str(parent)).returncode,0)
            self.assertEqual(run(qa_uid,proof_gid,"touch",str(qa/"active.json")).returncode,0)
            os.chmod(qa/"active.json",0o640)
            self.assertEqual(run(act_uid,proof_gid,"test","-r",str(qa/"active.json")).returncode,0)
            self.assertNotEqual(run(qa_uid,proof_gid,"touch",str(parent/"escape")).returncode,0)
            self.assertNotEqual(run(bad_uid,None,"test","-r",str(qa/"active.json")).returncode,0)

    def test_reconcile_spent_commit_finishes_without_reproving_and_detects_inconsistency(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg=ActivatorConfig.under(Path(temporary)); cfg.prepare(); seed=Activator(cfg,proof_reader=lambda k,e:dict(e))
            release=seed.test_release("g1",sequence=1); expected=seed._validate_release(release)
            atomic_json(cfg.active,expected); journal=cfg.journal/"j.json"
            atomic_json(journal,{"status":"prepared","requested":expected,"previous":None,"recorded_at":1})
            atomic_json(cfg.spent/(expected["nonce"]+".json"),{"sequence":1,"generation":"g1"})
            atomic_json(cfg.outcomes/(expected["nonce"]+".json"),{"status":"succeeded",**expected,"proved_at":1})
            Activator(cfg,proof_reader=lambda *_: (_ for _ in ()).throw(ActivationError("transient"))).reconcile()
            self.assertEqual(json.loads(journal.read_text())["status"],"succeeded")
            atomic_json(journal,{"status":"prepared","requested":expected,"previous":None,"recorded_at":1})
            atomic_json(cfg.spent/(expected["nonce"]+".json"),{"sequence":9,"generation":"wrong"})
            with self.assertRaisesRegex(ActivationError,"committed activation state inconsistent"):
                seed.reconcile()

    def test_publisher_loop_records_one_failure_and_continues_then_retries(self):
        decisions=[{"decision_id":"stale"},{"decision_id":"fresh"}]; calls=[]; states={}
        def worker(decision):
            calls.append(decision["decision_id"])
            if decision["decision_id"]=="stale": raise BundleError("non-fast-forward")
            return "pushed"
        process_records(decisions,worker,lambda key,value:states.__setitem__(key,value))
        self.assertEqual(calls,["stale","fresh"]); self.assertEqual(states["stale"]["status"],"failed")
        self.assertEqual(states["fresh"]["status"],"published")
        calls.clear(); process_records(decisions,worker,lambda key,value:states.__setitem__(key,value))
        self.assertEqual(calls,["stale","fresh"])

    def test_credential_validation_is_inode_safe_readable_and_owner_agnostic(self):
        from deploy.release_bundle import validate_systemd_credential
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"credential"; path.write_text("x"); os.chmod(path,0o400)
            validate_systemd_credential(path)
            link=path.with_name("link"); link.symlink_to(path)
            with self.assertRaises(BundleError): validate_systemd_credential(link)
            hard=path.with_name("hard"); os.link(path,hard)
            with self.assertRaises(BundleError): validate_systemd_credential(path)
        code=Path("deploy/release_bundle.py").read_text()
        self.assertNotIn("st_uid!=os.geteuid()",code)

    def test_credential_validation_accepts_real_systemd_load_credential_mode(self):
        """Real systemd LoadCredential= delivery on this host is root:root
        0440, not the 0400 this check originally required -- reproduced with
        `systemd-run -p LoadCredential=... -p User=dek-source-ingest`, which
        crash-looped dek-source-ingest-proof.service with "unsafe systemd
        credential" the first time this pipeline actually ran for real."""
        from deploy.release_bundle import validate_systemd_credential
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"credential"; path.write_text("x"); os.chmod(path,0o440)
            validate_systemd_credential(path)
            too_loose=Path(temporary)/"too-loose"; too_loose.write_text("x"); os.chmod(too_loose,0o444)
            with self.assertRaises(BundleError): validate_systemd_credential(too_loose)

    def test_git_environment_explicitly_disables_external_attributes_and_filters(self):
        code=Path("deploy/release_bundle.py").read_text()+Path("deploy/source_ingest_entrypoint.py").read_text()+Path("web/review.py").read_text()
        self.assertIn('"GIT_ATTR_NOSYSTEM":"1"',code)
        self.assertIn('"GIT_CONFIG_SYSTEM":"/dev/null"',code)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); repo=root/"repo"; repo.mkdir(); marker=root/"executed"
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            (repo/".gitattributes").write_text("*.txt filter=evil\n"); (repo/"a.txt").write_text("safe")
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@i","commit","-qm","x"],cwd=repo,check=True)
            global_cfg=root/"global"; global_cfg.write_text(f"[filter \"evil\"]\n clean = sh -c 'touch {marker}; cat'\n smudge = sh -c 'touch {marker}; cat'\n")
            module=__import__('deploy.release_bundle',fromlist=['GIT','_run'])
            module._run((*module.GIT,"-c","protocol.file.allow=always","clone","--",str(repo),str(root/"clone")),env={"GIT_CONFIG_GLOBAL":str(global_cfg)})
            self.assertFalse(marker.exists())

    def test_render_nonce_pins_snapshot_across_submit(self):
        class Source:
            def __init__(self,roots): self.roots=iter(roots)
            def current(self): return next(self.roots)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); one=root/"one"; two=root/"two"
            for directory,text in ((one,"one"),(two,"two")):
                (directory/"ingestion/rough").mkdir(parents=True); (directory/"ingestion/rough/a.md").write_text(f"---\nstatus: pending_review\nsource_item_key: sha256:{'1'*64}\n---\n{text}\n")
                subprocess.run(["git","init","-q"],cwd=directory,check=True); subprocess.run(["git","add","."],cwd=directory,check=True); subprocess.run(["git","-c","user.name=t","-c","user.email=t@i","commit","-qm","x"],cwd=directory,check=True)
            nonces=MemoryFormNonceStore(clock=lambda:1); service=__import__('web.review',fromlist=['ReviewService']).ReviewService(Source([one,two]),root/"q",audit_key=b"a"*16,queue_key=b"b"*16,nonces=nonces,clock=lambda:1)
            rendered=service.render("s").decode(); nonce=re.search('name="form_nonce" value="([^"]+)',rendered).group(1)
            binding=__import__('web.review',fromlist=['rough_binding_at']).rough_binding_at(one,PurePosixPath("ingestion/rough/a.md"))
            from urllib.parse import urlencode
            body=urlencode({"form_nonce":nonce,"rough_path":binding.path,"rough_sha256":binding.sha256,"rough_version":binding.version,"action":"reject","wiki_path":"","candidate_markdown":"","comment":"no"}).encode()
            service.submit_form(body,session_id="s",user_id="u")
            self.assertEqual(json.loads((root/"q").read_text())["snapshot_commit"],subprocess.check_output(["git","rev-parse","HEAD"],cwd=one,text=True).strip())

    def test_runbook_inventory_covers_git_auth_secrets_and_absent_cleanup(self):
        text=Path("deploy/PRODUCTION_ROLLOUT.md").read_text()
        for path in ("/var/lib/dek-git-auth","/var/lib/dek-publisher/secrets/approval-signing-key.pem","/var/lib/dek-publisher/secrets/review-decision-key","/var/lib/dek-review/secrets/review-decision-key","/var/lib/dek-web/secrets/generation-proof-secret","/var/lib/dek-activator/secrets/web-generation-proof"):
            self.assertIn(path,text)
        self.assertIn("deploy/rollback.py",text)
        self.assertIn("a6_cutover.py recover",text)
        self.assertIn("tree-manifest.json", text)
        self.assertIn("removes every inventory root", text)

class Round5SliceTests(unittest.TestCase):
    def test_append_supports_long_locked_records_and_reader_skips_corrupt_records(self):
        import unittest.mock as mock
        from web.review import append_record, iter_valid_decisions, decision_mac
        with tempfile.TemporaryDirectory() as temporary:
            q=Path(temporary)/"q.jsonl"; key=b"k"*32
            good={"record_type":"decision","decision_id":"good","action":"approve"}; good["decision_mac"]=decision_mac(good,key)
            append_record(q,good)
            long_record={"record_type":"decision","decision_id":"long","action":"approve","candidate_markdown":"x"*100_000}
            long_record["decision_mac"]=decision_mac(long_record,key)
            append_record(q,long_record)
            q.write_bytes(q.read_bytes()+b'{"truncated"\n'+b'{"record_type":"decision","decision_id":"bad","action":"approve","decision_mac":"hmac-sha256:00"}\n'+json.dumps(good).encode()+b"\n")
            self.assertEqual([x["decision_id"] for x in iter_valid_decisions(q,key)],["good","long","good"])

    def test_append_retries_short_writes_until_the_record_is_complete(self):
        import unittest.mock as mock
        from web.review import append_record
        with tempfile.TemporaryDirectory() as temporary:
            q=Path(temporary)/"q.jsonl"
            real_write=os.write
            def short_write(fd,payload):
                return real_write(fd,payload[:max(1,len(payload)//2)])
            with mock.patch("web.review.os.write",side_effect=short_write):
                append_record(q,{"record_type":"decision","candidate_markdown":"x"*10_000})
            parsed=json.loads(q.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed["candidate_markdown"]),10_000)

    def test_append_separates_a_crash_truncated_tail_from_the_next_record(self):
        from web.review import append_record, decision_mac, iter_valid_decisions
        with tempfile.TemporaryDirectory() as temporary:
            q=Path(temporary)/"q.jsonl"; key=b"k"*32
            q.write_bytes(b'{"record_type":"decision","candidate_markdown":"partial')
            good={"record_type":"decision","decision_id":"good","action":"approve"}
            good["decision_mac"]=decision_mac(good,key)
            append_record(q,good)
            self.assertEqual([item["decision_id"] for item in iter_valid_decisions(q,key)],["good"])

    def test_process_records_isolates_system_exit_but_not_keyboard_interrupt(self):
        calls=[]; states={}
        def worker(d):
            calls.append(d["decision_id"])
            if d["decision_id"]=="bad": raise SystemExit("missing archive")
            return "pushed"
        process_records([{"decision_id":"bad"},{"decision_id":"good"}],worker,lambda k,v:states.__setitem__(k,v))
        self.assertEqual(calls,["bad","good"]); self.assertEqual(states["bad"]["error_type"],"SystemExit")
        with self.assertRaises(KeyboardInterrupt): process_records([{"decision_id":"x"}],lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),lambda *_:None)

    def test_process_packages_prints_the_real_traceback_on_a_retryable_failure(self):
        """Same masking problem as process_records: builder_entrypoint.py's
        quarantine record was {"error_type": "BundleError"/"PermissionError",
        ...} with no message or traceback, and dek-builder.service itself
        exits 0 either way -- every real cause (a failing test, a permission
        bug) looked identical from the journal."""
        from deploy.builder_entrypoint import process_packages
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved"; approved.mkdir()
            builds = root / "builds"; builds.mkdir()
            failures = root / "failures"; failures.mkdir()
            package = approved / "pkg"; package.mkdir()
            (package / "approval.json").write_text(json.dumps({"decision_id": "d" * 20, "nonce": "n" * 20}))

            class FailingBuilder:
                def build(self, package, output):
                    raise PermissionError("[Errno 13] Permission denied: '/var/spool/dek-build/approved/.builder-failures'")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                process_packages(FailingBuilder(), approved, builds, failures)
            self.assertIn("PermissionError", stderr.getvalue())
            self.assertIn(".builder-failures", stderr.getvalue())

    def test_process_records_prints_the_real_traceback_on_a_retryable_failure(self):
        """error_type alone (e.g. "PermissionError", with no message or
        traceback) gave no way to diagnose a real production failure from
        the journal -- every decision looked like a generic retryable
        failure while dek-review-publish.service itself exited 0, masking
        the actual cause behind an apparently-successful oneshot unit."""
        def worker(d): raise PermissionError("[Errno 13] Permission denied: '/var/lib/dek-activate/outcomes'")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            process_records([{"decision_id":"bad"}], worker, lambda k, v: None)
        self.assertIn("PermissionError", stderr.getvalue())
        self.assertIn("/var/lib/dek-activate/outcomes", stderr.getvalue())

    def test_spent_without_outcome_is_reconstructed_and_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg=ActivatorConfig.under(Path(temporary)); cfg.prepare(); a=Activator(cfg,proof_reader=lambda *_: (_ for _ in ()).throw(AssertionError("must not reprove")),clock=lambda:7)
            release=a.test_release("g1",sequence=1); expected=a._validate_release(release)
            atomic_json(cfg.active,expected); journal=cfg.journal/"j.json"; atomic_json(journal,{"status":"prepared","requested":expected,"previous":None,"recorded_at":1})
            atomic_json(cfg.spent/(expected["nonce"]+".json"),{"sequence":1,"generation":"g1"})
            a.reconcile(); self.assertEqual(json.loads((cfg.outcomes/(expected["nonce"]+".json")).read_text())["proved_at"],7)
            self.assertEqual(json.loads(journal.read_text())["status"],"succeeded")
            (cfg.outcomes/(expected["nonce"]+".json")).unlink(); atomic_json(journal,{"status":"prepared","requested":{**expected,"tree":"9"*40},"previous":None,"recorded_at":1})
            with self.assertRaises(ActivationError): a.reconcile()

    def test_builder_output_is_traversable_readonly_by_real_activator_identity(self):
        code=Path("deploy/builder_entrypoint.py").read_text()
        self.assertIn("make_activator_readable",code)
        if os.geteuid()==0 and shutil.which("setpriv"):
            with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
                root=Path(temporary); os.chmod(root,0o755); product=root/"actual"
                class B:
                    def build(self,p,t):
                        (t/"site").mkdir(parents=True); (t/"site/index.html").write_text("x"); (t/"release.json").write_text("{}")
                build_atomically(B(),root,product)
                uid=65103
                run=lambda *x: subprocess.run(["setpriv",f"--reuid={uid}",f"--regid={uid}","--clear-groups",*x]).returncode
                self.assertEqual(run("test","-r",str(product/"release.json")),0)
                self.assertEqual(run("test","-x",str(product/"site")),0)
                self.assertNotEqual(run("test","-w",str(product/"release.json")),0)

    def test_activator_entrypoint_scan_skips_dotfiles_and_incomplete_dirs(self):
        # BundleBuilder writes its own failure quarantine at builds/.builder-failures
        # (a real directory dek-activator's identity cannot read, by design). The
        # scan must skip dot-prefixed entries the same way builder_entrypoint.py's
        # own package scan already does -- without this, a single quarantined
        # failure sitting alongside real candidates crashes every activator run
        # with PermissionError, blocking activation entirely.
        from deploy.activator_entrypoint import _scan_candidate_dirs
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hidden = root / ".builder-failures"; hidden.mkdir(mode=0o700)
            (hidden / "activation-ready.json").write_text("{}")
            (hidden / "activation-ready.sig").write_bytes(b"x")
            os.chmod(hidden, 0o000)
            incomplete = root / "incomplete-candidate"; incomplete.mkdir()
            (incomplete / "activation-ready.json").write_text("{}")
            real = root / "real-candidate"; real.mkdir()
            (real / "activation-ready.json").write_text("{}")
            (real / "activation-ready.sig").write_bytes(b"x")
            try:
                self.assertEqual(_scan_candidate_dirs(root), [real])
            finally:
                os.chmod(hidden, 0o700)

    def test_activator_entrypoint_orders_and_isolates_pending(self):
        from deploy.activator_entrypoint import activate_candidates
        calls=[]
        class A:
            def activate(self,p):
                calls.append(p.name)
                if p.name=="a": raise ActivationError("bad")
        activate_candidates(A(),[("d2","n", "b",Path("b")),("d1","n","a",Path("a"))])
        self.assertEqual(calls,["a","b"])

    def test_activator_entrypoint_fails_when_every_pending_candidate_fails(self):
        from deploy.activator_entrypoint import activate_candidates
        class A:
            def activate(self,p):
                raise ActivationError("bad signature")
        with self.assertRaisesRegex(ActivationError,"all pending activations failed"):
            activate_candidates(A(),[("d1","n","a",Path("a"))])

    def test_runbook_has_executable_seed_nginx_qa_and_rollback_gates(self):
        text=Path("deploy/PRODUCTION_ROLLOUT.md").read_text()
        for token in ("DEK_INITIAL_RELEASE","git bundle verify","release.sig","os.replace","nginx -t","https://regkb.chenponai.com/review/auth/callback","/etc/nginx/snippets/dek-review-location.conf","QA_HERMES_LOCK","python3.12","pip check","sha256sum -c /root/dek-backup-SHA256SUMS","tar --xattrs --acls --numeric-owner -xpf","unit-enablement.before","rollback.py"):
            self.assertIn(token,text)
        self.assertTrue(Path("deploy/nginx/dek-review.conf").is_file())
        self.assertTrue(Path("deploy/nginx/dek-review-location.conf").is_file())
        self.assertTrue(Path("deploy/rollback.py").is_file())


class Round9SliceTests(unittest.TestCase):
    """Round 9: publisher finalize overwrites the builder-owned release.json without EPERM.

    Cross-component DAC contract: the builder's ``make_activator_readable()`` is the sole
    source of ``release.json``'s activator-readable 0664 mode.  Overwriting a file in place
    (``write_text`` truncates, it does not recreate the inode) never changes its mode, and
    the publisher is not ``release.json``'s owner (``dek-builder`` owns it), so the publisher
    MUST NOT ``chmod`` ``release.json`` -- that would raise EPERM without ``CAP_FOWNER`` and
    block every finalize.  The publisher only ``chmod``s ``release.sig``, which it created
    and therefore owns.
    """

    REPO_ROOT = str(Path(__file__).resolve().parents[2])

    @staticmethod
    def _build_package(root: Path, *, with_bundle: bool = False):
        from deploy.release_bundle import _canonical
        key = Ed25519PrivateKey.generate()
        package = root / "package"
        package.mkdir()
        bundle_sha = None
        if with_bundle:
            bundle = package / "repository.bundle"
            bundle.write_bytes(b"fake-bundle")
            bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        approval = {"schema_version": 2, "decision_id": "decision-12345678", "decision_sha256": "1" * 64,
                    "nonce": "decision-12345678", "origin": "https://github.com/chenponsh/dek.git",
                    "commit": "2" * 40, "tree": "3" * 40, "bundle_sha256": bundle_sha or ("4" * 64)}
        (package / "approval.json").write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")) + "\n")
        (package / "approval.sig").write_bytes(key.sign(_canonical(approval)))
        (package / "site").mkdir()
        (package / "site/index.html").write_text("ok")
        (package / "dek-kb.json").write_text('{"version":4,"documents":[]}')
        artifacts = {"dek-kb.json": hashlib.sha256((package / "dek-kb.json").read_bytes()).hexdigest(),
                     "site/index.html": hashlib.sha256((package / "site/index.html").read_bytes()).hexdigest()}
        claim = {**approval, "generation": "decision-12345678-decision-12345678", "artifacts": artifacts}
        (package / "release.json").write_text(json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n")
        key_pem = root / "signing-key.pem"
        key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        return key, package, approval, artifacts, key_pem

    def _finalize_as(self, package: Path, key_pem: Path, uid: int, write_gid: int, *, in_group: bool):
        """Run ``ReleasePublisher.finalize`` as a real non-root identity via ``setpriv``."""
        runner = package.parent / "finalize_runner.py"
        runner.write_text(
            "import sys, json\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {self.REPO_ROOT!r})\n"
            "from deploy.release_bundle import ReleasePublisher\n"
            "from cryptography.hazmat.primitives.serialization import load_pem_private_key\n"
            "pkg = Path(sys.argv[1])\n"
            "key = load_pem_private_key(Path(sys.argv[2]).read_bytes(), password=None)\n"
            "publisher = object.__new__(ReleasePublisher)\n"
            "publisher.signing_key = key\n"
            "final = publisher.finalize(pkg)\n"
            "print(json.dumps({'generation': final['generation'], 'artifacts': final['artifacts']}, sort_keys=True))\n",
            encoding="utf-8",
        )
        os.chmod(runner, 0o644)
        groups = [f"--groups={write_gid}"] if in_group else ["--clear-groups"]
        command = ["setpriv", f"--reuid={uid}", f"--regid={uid}", *groups,
                   "/usr/bin/python3", str(runner), str(package), str(key_pem)]
        return subprocess.run(command, cwd=self.REPO_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def test_finalize_chmods_only_publisher_owned_release_sig(self):
        code = Path("deploy/release_bundle.py").read_text(encoding="utf-8")
        self.assertIn("os.chmod(release_sig", code)
        self.assertNotIn("os.chmod(release_json", code)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "requires root and setpriv")
    def test_finalize_as_publisher_overwrites_builder_release_json_without_eperm_and_preserves_mode(self):
        import stat
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o755)  # temp root is 0700; make it traversable for the real identity
            key, package, approval, artifacts, key_pem = self._build_package(root)
            builder_uid, publisher_uid, write_gid = 69201, 69202, 69210
            # Realistic build-to-activator ownership: dek-builder:dek-build-write, setgid 2775.
            # release.json arrives 0664 from the builder's make_activator_readable().
            for path in [package, *package.rglob("*")]:
                os.chown(path, builder_uid, write_gid)
                os.chmod(path, 0o2775 if path.is_dir() else 0o664)
            self.assertEqual(stat.S_IMODE((package / "release.json").stat().st_mode), 0o664,
                             "builder must preset release.json to 0664")
            untouched = {}
            for name in ("approval.json", "approval.sig", "dek-kb.json", "site/index.html"):
                p = package / name
                untouched[name] = (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))

            # The publisher is NOT release.json's owner and has no CAP_FOWNER: the removed
            # chmod(release.json) is exactly what used to raise EPERM here.
            proc = self._finalize_as(package, key_pem, publisher_uid, write_gid, in_group=True)
            self.assertEqual(proc.returncode, 0, f"finalize must not EPERM: {proc.stderr}")

            # release.json: overwritten in place by the publisher, mode unchanged (builder preset 0664).
            self.assertEqual(stat.S_IMODE((package / "release.json").stat().st_mode), 0o664,
                             "release.json must keep builder-preset 0664 across the overwrite")
            final = json.loads((package / "release.json").read_text(encoding="utf-8"))
            self.assertEqual(final["generation"], "decision-12345678-decision-12345678")
            self.assertEqual(final["artifacts"], artifacts)

            # release.sig: publisher-created and publisher-owned, chmod 0664 (other-read,
            # group-write, owner-write; never other-write).
            sig = package / "release.sig"
            self.assertTrue(sig.is_file())
            sig_mode = stat.S_IMODE(sig.stat().st_mode)
            self.assertEqual(sig_mode & 0o004, 0o004, "release.sig must be other-readable")
            self.assertEqual(sig_mode & 0o002, 0, "release.sig must not be other-writable")
            self.assertEqual(sig_mode & 0o020, 0o020, "release.sig must be group-writable")
            self.assertEqual(sig_mode & 0o200, 0o200, "release.sig must be owner-writable")
            key.public_key().verify(sig.read_bytes(), _canonical_release(final))

            # approval.* and the static artifacts are untouched by finalize.
            for name, (digest, mode) in untouched.items():
                p = package / name
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest, f"{name} content untouched")
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), mode, f"{name} mode untouched")

    def test_publish_does_not_reset_finalized_release_permissions(self):
        import stat
        import unittest.mock as mock
        import deploy.release_bundle as rb
        with tempfile.TemporaryDirectory() as temporary:
            key, package, approval, artifacts, _ = self._build_package(Path(temporary), with_bundle=True)
            publisher = object.__new__(ReleasePublisher)
            publisher.signing_key = key
            credential = Path(temporary) / "git-credentials"
            credential.write_text("https://alice:s3cret@github.com/chenponsh/dek.git")
            publisher.origin = "https://github.com/chenponsh/dek.git"
            publisher.credential_file = credential
            old = os.umask(0o007)
            try:
                publisher.finalize(package)
            finally:
                os.umask(old)
            modes = {n: stat.S_IMODE((package / n).stat().st_mode) for n in ("release.json", "release.sig")}
            digests = {n: hashlib.sha256((package / n).read_bytes()).hexdigest() for n in ("release.json", "release.sig")}
            commit, tree = approval["commit"], approval["tree"]

            def fake_run(arguments, *, cwd=None, env=None, timeout=120):
                args = list(arguments)
                if "push" in args:
                    return b""
                if "clone" in args:
                    return b""
                if "rev-parse" in args:
                    last = args[-1]
                    if last.endswith("^{tree}"):
                        return tree.encode()
                    if last.endswith("^{commit}"):
                        return commit.encode()
                raise AssertionError(f"unexpected git command: {arguments}")

            with mock.patch.object(rb, "_run", side_effect=fake_run):
                publisher.publish(package)
            for name in ("release.json", "release.sig"):
                self.assertEqual(stat.S_IMODE((package / name).stat().st_mode), modes[name],
                                 f"{name} mode must survive publish")
                self.assertEqual(hashlib.sha256((package / name).read_bytes()).hexdigest(), digests[name],
                                 f"{name} content must survive publish")
