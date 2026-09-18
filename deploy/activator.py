"""Non-root, fixed-function activation of immutable static releases."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from deploy.release_bundle import BundleError, GENERATION_PATTERN, _canonical, _canonical_activation_ready, _canonical_release, approval_generation
from deploy.fsutil import atomic_write_json, fsync_dir


ID = re.compile(r"[A-Za-z0-9_-]{2,160}")
HEX = re.compile(r"[0-9a-f]{40,64}")
STATIC_SUFFIXES = {".html", ".css", ".js", ".json", ".map", ".txt", ".xml", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}


class ActivationError(RuntimeError):
    pass


class FatalActivationError(ActivationError):
    """Global state is unsafe; no later candidate may be attempted."""
    pass


def _fsync_dir(path: Path) -> None:
    fsync_dir(path)


def fsync_tree(root: Path) -> None:
    """Durably flush every regular file and directory, leaves first."""
    root = Path(root)
    files = []
    directories = []
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISREG(details.st_mode):
            files.append(path)
        elif stat.S_ISDIR(details.st_mode):
            directories.append(path)
        else:
            raise ActivationError("release contains unsafe entry")
    for path in sorted(files):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        _fsync_dir(path)
    _fsync_dir(root)


def atomic_json(path: Path, value: dict, mode: int = 0o640) -> None:
    # 0o777 (subject to umask) reproduces plain Path.mkdir()'s default mode --
    # this call does not intend to change the parent directory's permissions.
    atomic_write_json(path, value, mode=mode, ensure_parent_mode=0o777)


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""): value.update(chunk)
    return value.hexdigest()


@dataclass(frozen=True)
class ActivatorConfig:
    build_inbox: Path
    releases: Path
    control: Path
    journal: Path
    outcomes: Path
    spent: Path
    active: Path
    retain: int = 5

    @classmethod
    def under(cls, root: Path) -> "ActivatorConfig":
        root = Path(root)
        return cls(root/"build", root/"releases", root/"control", root/"journal", root/"outcomes", root/"spent", root/"control/active.json")

    def prepare(self) -> None:
        for path in (self.build_inbox, self.releases, self.control, self.journal, self.outcomes, self.spent):
            path.mkdir(parents=True, exist_ok=True)


class Activator:
    def __init__(self, config: ActivatorConfig, *, proof_reader: Callable[[str, dict], dict], approval_key: Ed25519PublicKey | None = None, clock=time.time):
        self.config, self.proof_reader, self.approval_key, self.clock = config, proof_reader, approval_key, clock

    def test_release(self, generation: str, *, sequence: int) -> Path:
        """Test fixture helper that still produces the production metadata shape."""
        release = self.config.build_inbox / generation
        (release / "site").mkdir(parents=True)
        (release / "site/index.html").write_text(generation, encoding="utf-8")
        (release / "dek-kb.json").write_text('{"version":4,"documents":[]}', encoding="utf-8")
        previous = self._read_active(required=False)
        metadata = {"schema_version":2, "sequence":sequence, "nonce":"nonce-"+generation+"-12345678",
                    "generation":generation, "previous_generation":previous.get("generation") if previous else None,
                    "commit":"1"*40, "tree":"2"*40, "bundle_sha256":"3"*64,
                    "artifacts":{"dek-kb.json":file_digest(release/"dek-kb.json"), "site/index.html":file_digest(release/"site/index.html")}}
        (release / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        return release

    def _read_active(self, *, required=True) -> dict | None:
        try:
            raw = self.config.active.read_bytes()
            if len(raw) > 8 * 1024 * 1024: raise ActivationError("active metadata too large")
            value = json.loads(raw)
            self._validate_metadata(value)
            return value
        except FileNotFoundError:
            if required: raise ActivationError("active generation missing")
            return None
        except ActivationError as exc:
            raise FatalActivationError("committed active state inconsistent") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise FatalActivationError("committed active state inconsistent") from exc

    @staticmethod
    def _validate_metadata(value: object) -> None:
        core = {"schema_version","sequence","nonce","generation","previous_generation","commit","tree","bundle_sha256","artifacts"}
        provenance={"decision_id","decision_sha256","origin"}
        optional_ancestry={"parent_commit"}
        fields=set(value) if isinstance(value,dict) else set()
        if (not isinstance(value, dict) or fields not in (core, core|provenance, core|provenance|optional_ancestry) or value["schema_version"] != 2
                or type(value["sequence"]) is not int or value["sequence"] < 1
                or not ID.fullmatch(str(value["nonce"])) or not ID.fullmatch(str(value["generation"]))
                or (value["previous_generation"] is not None and not ID.fullmatch(str(value["previous_generation"])))
                or not HEX.fullmatch(str(value["commit"])) or not HEX.fullmatch(str(value["tree"]))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value["bundle_sha256"]))
                or not isinstance(value["artifacts"], dict)):
            raise ActivationError("release metadata invalid")
        if "parent_commit" in value and not HEX.fullmatch(str(value["parent_commit"])):
            raise ActivationError("release ancestry invalid")
        if (not {"dek-kb.json", "site/index.html"}.issubset(value["artifacts"])
                or any((key != "dek-kb.json" and not key.startswith("site/")) or ".." in Path(key).parts for key in value["artifacts"])
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(v)) for v in value["artifacts"].values())):
            raise ActivationError("artifact inventory invalid")

    def _validate_release(self, source: Path, *, confined: bool = True) -> dict:
        source = source.resolve(strict=True)
        if confined and (source.parent != self.config.build_inbox.resolve(strict=True) or source.is_symlink()):
            raise ActivationError("release escaped build inbox")
        metadata = json.loads((source/"release.json").read_text(encoding="utf-8"))
        files = []
        for path in source.rglob("*"):
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or (not path.is_dir() and (not path.is_file() or details.st_nlink != 1)):
                raise ActivationError("release contains unsafe entry")
            if path.is_file(): files.append(path.relative_to(source).as_posix())
        if "dek-kb.json" not in files or "site/index.html" not in files or "release.json" not in files:
            raise ActivationError("release is incomplete")
        if any(p not in {"dek-kb.json","release.json","build-release.json","approval.json","approval.sig","release.sig","activation-ready.json","activation-ready.sig","release.lock","repository.bundle"} and (not p.startswith("site/") or Path(p).suffix.lower() not in STATIC_SUFFIXES) for p in files):
            raise ActivationError("release contains executable content")
        if "sequence" in metadata:
            self._validate_metadata(metadata)
        else:
            required={"schema_version","decision_id","decision_sha256","nonce","origin","commit","tree","bundle_sha256","generation","artifacts"}
            if not required.issubset(metadata) or metadata.get("schema_version")!=2: raise ActivationError("builder metadata invalid")
            try: generation=approval_generation(metadata.get("decision_id"), metadata.get("nonce"))
            except BundleError as exc: raise ActivationError("builder identity invalid") from exc
            if metadata.get("generation") != generation or not GENERATION_PATTERN.fullmatch(generation): raise ActivationError("builder identity invalid")
            if not HEX.fullmatch(str(metadata.get("commit",""))) or not HEX.fullmatch(str(metadata.get("tree",""))) or not re.fullmatch(r"[0-9a-f]{64}",str(metadata.get("bundle_sha256",""))): raise ActivationError("builder object binding invalid")
            if self.approval_key is None: raise ActivationError("approval verification key missing")
            try:
                approval=json.loads((source/"approval.json").read_text(encoding="utf-8"))
                self.approval_key.verify((source/"approval.sig").read_bytes(),_canonical(approval))
                self.approval_key.verify((source/"release.sig").read_bytes(),_canonical_release(metadata))
            except Exception as exc: raise ActivationError("signed release approval invalid") from exc
            for key in ("decision_id","decision_sha256","nonce","origin","commit","tree","bundle_sha256"):
                if metadata.get(key)!=approval.get(key): raise ActivationError("release is not approval-bound")
            try: approved_generation=approval_generation(approval.get("decision_id"), approval.get("nonce"))
            except BundleError as exc: raise ActivationError("builder identity invalid") from exc
            if metadata.get("generation") != approved_generation: raise ActivationError("generation is not decision-derived")
            try:
                gate=json.loads((source/"activation-ready.json").read_text(encoding="utf-8"))
                self.approval_key.verify((source/"activation-ready.sig").read_bytes(),_canonical_activation_ready(gate))
            except Exception as exc:
                raise ActivationError("activation gate invalid") from exc
            expected_gate={"schema_version":1,"status":"pushed","generation":metadata["generation"],
                           "commit":metadata["commit"],"tree":metadata["tree"],"bundle_sha256":metadata["bundle_sha256"],
                           "release_sha256":file_digest(source/"release.json"),
                           "release_signature_sha256":file_digest(source/"release.sig")}
            if "parent_commit" in metadata:
                expected_gate["parent_commit"] = metadata["parent_commit"]
            queue_fields={"decision_queue_sha256","decision_queue_size"}
            if queue_fields.issubset(gate):
                if (not re.fullmatch(r"[0-9a-f]{64}",str(gate.get("decision_queue_sha256","")))
                        or type(gate.get("decision_queue_size")) is not int or gate["decision_queue_size"] < 0):
                    raise ActivationError("activation gate queue binding invalid")
                expected_gate.update({name:gate[name] for name in queue_fields})
            if gate != expected_gate: raise ActivationError("activation gate is not release-bound")
            static = {p for p in files if p == "dek-kb.json" or p.startswith("site/")}
            if set(metadata["artifacts"]) != static: raise ActivationError("artifact inventory incomplete")
        for relative, expected in metadata["artifacts"].items():
            if file_digest(source/relative) != expected: raise ActivationError("artifact digest mismatch")
        return metadata

    @staticmethod
    def _matching_proof(proof: dict, expected: dict) -> bool:
        keys = ("sequence","nonce","generation","commit","tree","bundle_sha256","artifacts")
        return isinstance(proof, dict) and all(proof.get(key) == expected[key] for key in keys)

    def _prove(self, expected: dict) -> None:
        for kind in ("web", "qa"):
            if not self._matching_proof(self.proof_reader(kind, expected), expected):
                raise ActivationError(f"fresh {kind} proof mismatch")

    def _failed_path(self, expected: dict) -> Path:
        return self.config.control / "failed" / (expected["nonce"] + ".json")

    def _quarantine_failed(self, expected: dict) -> None:
        atomic_json(self._failed_path(expected), {
            "schema_version": 1, "status": "quarantined",
            "nonce": expected["nonce"], "generation": expected["generation"],
            "commit": expected["commit"], "tree": expected["tree"],
            "bundle_sha256": expected["bundle_sha256"],
        })

    def _remove_active_if_candidate(self, expected: dict) -> bool:
        observed = self._read_active(required=False)
        if observed != expected:
            return False
        try: self.config.active.unlink()
        except FileNotFoundError: return False
        _fsync_dir(self.config.active.parent)
        return True

    def activate(self, source: Path) -> dict:
        source_metadata = self._validate_release(source)
        expected = source_metadata
        previous = self._read_active(required=False)
        if "sequence" not in expected:
            expected={"schema_version":2,"sequence":1 if previous is None else previous["sequence"]+1,
                      "nonce":expected["nonce"],"generation":expected["generation"],
                      "previous_generation":previous.get("generation") if previous else None,
                      "commit":expected["commit"],"tree":expected["tree"],"bundle_sha256":expected["bundle_sha256"],"artifacts":expected["artifacts"],
                      "decision_id":expected["decision_id"],"decision_sha256":expected["decision_sha256"],"origin":expected["origin"]}
            if "parent_commit" in source_metadata:
                expected["parent_commit"] = source_metadata["parent_commit"]
        if self._failed_path(expected).exists(): raise ActivationError("quarantined candidate")
        if (self.config.spent/(expected["nonce"]+".json")).exists(): raise ActivationError("spent nonce")
        if expected["previous_generation"] != (previous.get("generation") if previous else None): raise ActivationError("previous generation mismatch")
        if previous and expected["sequence"] != previous["sequence"] + 1: raise ActivationError("activation sequence mismatch")
        # previous lacking "decision_id" means it's a from-source seed (or a
        # pre-review-pipeline rollback target), never itself a reviewed
        # decision -- no real decision's parent_commit can ever equal its
        # commit, since every commit since bootstrap moved the repo forward
        # without that generation's involvement. Only enforce the strict
        # chain once the generation being superseded was itself decision-derived.
        if previous and "decision_id" in expected and "decision_id" in previous and expected.get("parent_commit") != previous.get("commit"):
            raise ActivationError("publication ancestry mismatch")
        journal_path = self.config.journal / (f'{expected["sequence"]:020d}-{expected["nonce"]}.json')
        journal = {"status":"prepared", "requested":expected, "previous":previous, "recorded_at":int(self.clock())}
        atomic_json(journal_path, journal)  # durable before the first mutation
        target = self.config.releases / expected["generation"]
        if not target.exists():
            staging=Path(tempfile.mkdtemp(prefix=".ingest-",dir=self.config.releases)); shutil.rmtree(staging)
            try:
                shutil.copytree(source, staging, copy_function=shutil.copy2)
                if self._validate_release(staging, confined=False) != source_metadata: raise ActivationError("copied release metadata changed")
                # shutil.copytree's final copystat(source, staging) matches
                # staging's mode -- including the setgid bit -- to source's,
                # which is not setgid. Any file written into staging after
                # this point loses the group inherited from self.config.releases
                # (still setgid) instead of getting it. dek-web later can't
                # read a group-mismatched release.lock/release.json and every
                # request 503s. Write these two files into a fresh sibling
                # under self.config.releases (still setgid, so still correctly
                # group-inherited) and move them in -- os.replace preserves
                # the group the file was created with, unlike shutil.copy2.
                meta=Path(tempfile.mkdtemp(prefix=".ingest-meta-",dir=self.config.releases))
                try:
                    if json.loads((staging/"release.json").read_text(encoding="utf-8")) != expected:
                        atomic_json(meta/"release.json",expected,0o440)
                        os.replace(staging/"release.json",staging/"build-release.json")
                        os.replace(meta/"release.json",staging/"release.json")
                    (meta/"release.lock").touch(exist_ok=True); os.chmod(meta/"release.lock",0o440)
                    os.replace(meta/"release.lock",staging/"release.lock")
                finally:
                    shutil.rmtree(meta, ignore_errors=True)
                for path in sorted(staging.rglob("*"), reverse=True): os.chmod(path, 0o550 if path.is_dir() else 0o440)
                os.chmod(staging,0o550)
                fsync_tree(staging)
                os.replace(staging,target)
            except Exception:
                if staging.exists():
                    # The chmod loop above (0o550/0o440, read-only) runs before
                    # fsync_tree(); a failure after that point means rmtree must
                    # delete read-only files/dirs it just locked down. As root
                    # that's invisible (DAC_OVERRIDE bypasses the mode bits
                    # entirely); dek-activator's real, non-root identity would
                    # get EPERM here instead of the actual underlying error.
                    for path in sorted(staging.rglob("*"), reverse=True):
                        os.chmod(path, 0o770 if path.is_dir() else 0o660)
                    os.chmod(staging, 0o770)
                    shutil.rmtree(staging)
                raise
            _fsync_dir(self.config.releases)
        else:
            existing=json.loads((target/"release.json").read_text(encoding="utf-8")); self._validate_metadata(existing)
            if existing!=expected: raise FatalActivationError("committed generation identity collision")
            for relative,digest in expected["artifacts"].items():
                if file_digest(target/relative)!=digest: raise FatalActivationError("committed generation digest mismatch")
        atomic_json(self.config.active, expected)
        try:
            self._prove(expected)
        except Exception as exc:
            observed = self._read_active()
            if observed != expected:
                journal["status"] = "stopped_unknown_generation"; atomic_json(journal_path, journal)
                raise FatalActivationError("unknown third generation; rollback stopped") from exc
            if previous is None:
                self._quarantine_failed(expected)
                if not self._remove_active_if_candidate(expected):
                    journal["status"] = "stopped_unknown_generation"; atomic_json(journal_path, journal)
                    raise FatalActivationError("unknown third generation; rollback stopped") from exc
                journal["status"] = "failed_no_previous"; atomic_json(journal_path, journal)
                raise ActivationError("activation failed and no rollback generation exists") from exc
            atomic_json(self.config.active, previous)
            self._prove(previous)
            journal["status"] = "rolled_back"; atomic_json(journal_path, journal)
            raise ActivationError("activation failed; old generation restored") from exc
        atomic_json(self.config.spent/(expected["nonce"]+".json"), {"sequence":expected["sequence"], "generation":expected["generation"]})
        outcome = {"status":"succeeded", **expected, "proved_at":int(self.clock())}
        atomic_json(self.config.outcomes/(expected["nonce"]+".json"), outcome)
        journal["status"] = "succeeded"; atomic_json(journal_path, journal)
        self.cleanup()
        return outcome

    def cleanup(self) -> None:
        active = self._read_active(required=False)
        protected = {active.get("generation"), active.get("previous_generation")} if active else set()
        releases = sorted((p for p in self.config.releases.iterdir() if p.is_dir()), key=lambda p:p.stat().st_mtime_ns, reverse=True)
        keep = protected | {path.name for path in releases[:self.config.retain]}
        for path in releases:
            if path.name not in keep:
                lock=os.open(path/"release.lock",os.O_RDONLY|os.O_CREAT,0o440)
                try:
                    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError: continue
                    for child in path.rglob("*"):
                        if child.is_dir(): os.chmod(child,0o700)
                    os.chmod(path,0o700); shutil.rmtree(path)
                finally:
                    os.close(lock)

    def reconcile(self) -> None:
        """Deterministically preserve terminal journals and fail closed on interrupted mutation."""
        for path in sorted(self.config.journal.glob("*.json")):
            active = self._read_active(required=False)
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise FatalActivationError("committed activation state inconsistent") from exc
            if value.get("status") == "prepared":
                requested = value.get("requested", {}); previous = value.get("previous")
                spent_path=self.config.spent/(str(requested.get("nonce"))+".json")
                if spent_path.exists():
                    try:
                        spent=json.loads(spent_path.read_text(encoding="utf-8"))
                    except (OSError,KeyError,ValueError,json.JSONDecodeError) as exc: raise FatalActivationError("committed activation state inconsistent") from exc
                    if active!=requested or spent!={"sequence":requested.get("sequence"),"generation":requested.get("generation")}:
                        raise FatalActivationError("committed activation state inconsistent")
                    outcome_path=self.config.outcomes/(requested["nonce"]+".json")
                    if outcome_path.exists():
                        try: outcome=json.loads(outcome_path.read_text(encoding="utf-8"))
                        except (OSError,ValueError,json.JSONDecodeError) as exc: raise FatalActivationError("committed activation state inconsistent") from exc
                        if outcome.get("status")!="succeeded" or any(outcome.get(k)!=v for k,v in requested.items()): raise FatalActivationError("committed activation state inconsistent")
                    else:
                        atomic_json(outcome_path,{"status":"succeeded",**requested,"proved_at":int(self.clock())})
                    value["status"]="succeeded"; atomic_json(path,value); continue
                if active == requested:
                    if previous is None and self._failed_path(requested).exists():
                        if not self._remove_active_if_candidate(requested):
                            value["status"]="stopped_unknown_generation"; atomic_json(path,value)
                            raise FatalActivationError("unknown third generation; reconciliation stopped")
                        value["status"]="failed_no_previous"; atomic_json(path,value); continue
                    try:
                        self._prove(requested); value["status"] = "succeeded"
                        atomic_json(self.config.spent/(requested["nonce"]+".json"), {"sequence":requested["sequence"], "generation":requested["generation"]})
                        atomic_json(self.config.outcomes/(requested["nonce"]+".json"),{"status":"succeeded",**requested,"proved_at":int(self.clock())})
                    except Exception as exc:
                        observed=self._read_active()
                        if observed != requested:
                            value["status"]="stopped_unknown_generation"; atomic_json(path,value)
                            raise FatalActivationError("unknown third generation; reconciliation stopped") from exc
                        if previous is None:
                            self._quarantine_failed(requested)
                            if not self._remove_active_if_candidate(requested):
                                value["status"]="stopped_unknown_generation"; atomic_json(path,value)
                                raise FatalActivationError("unknown third generation; reconciliation stopped") from exc
                            value["status"]="failed_no_previous"; atomic_json(path,value); continue
                        atomic_json(self.config.active,previous); self._prove(previous); value["status"]="rolled_back"
                elif active == previous:
                    if previous is None:
                        self._quarantine_failed(requested); value["status"]="failed_no_previous"
                    else: self._prove(previous); value["status"] = "rolled_back"
                else:
                    value["status"] = "stopped_unknown_generation"
                    atomic_json(path, value)
                    raise FatalActivationError("unknown third generation; reconciliation stopped")
                atomic_json(path, value)
