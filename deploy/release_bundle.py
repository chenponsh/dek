"""Approval-bound ``git bundle`` producer and credentialless snapshot builder."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import base64
from urllib.parse import urlsplit
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from deploy.fsutil import atomic_write_bytes


class BundleError(RuntimeError): pass

STATIC_SUFFIXES = {".html", ".css", ".js", ".json", ".map", ".txt", ".xml", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}
GIT = ("/usr/bin/git", "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "credential.interactive=never", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=", "-c", "diff.external=", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always")
APPROVAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,79}$")
GENERATION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{17,159}$")


def approval_generation(decision_id: object, nonce: object) -> str:
    """Return the single filesystem-safe generation representation."""
    if (not isinstance(decision_id, str) or not APPROVAL_ID_PATTERN.fullmatch(decision_id)
            or not isinstance(nonce, str) or not APPROVAL_ID_PATTERN.fullmatch(nonce)):
        raise BundleError("unsafe approval identity")
    generation = f"{decision_id}-{nonce}"
    if not GENERATION_PATTERN.fullmatch(generation):
        raise BundleError("unsafe generation identity")
    return generation


PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


def _proxy_env() -> dict:
    """Proxy settings for outbound git network calls, read from this process's
    own environment (set by the systemd unit), never from caller-supplied data."""
    return {key: os.environ[key] for key in PROXY_ENV_KEYS if key in os.environ}


def _run(arguments, *, cwd: Path | None = None, env=None, timeout=120) -> bytes:
    # FIXED_COMMANDS' web/tests needs `node` (test_search.py runs search.js for
    # real) and qa/tests needs `uv` (test_dependency_lock.py); neither lives
    # under /usr/bin or /bin on this host, and their real locations
    # (/usr/local/bin/node, /root/.local/bin/uv) are symlinks/paths into
    # /root, which this sandbox hides (ProtectHome=true plus an explicit
    # InaccessiblePaths=/root) -- a hole into the operator's home directory
    # is the wrong fix, so standalone copies of both are vendored to
    # /opt/dek-vendor/bin instead, a location the sandbox can actually read
    # (see deploy/systemd/dek-builder.service's ReadOnlyPaths=).
    #
    # dek-builder.service is also PrivateNetwork=true (fully networkless),
    # so qa/tests' uv-based dependency-lock check can only work against a
    # pre-warmed, offline cache -- never a live resolve. UV_CACHE_DIR points
    # at that cache (populated once, with network, as a one-time vendoring
    # step) and UV_OFFLINE forces uv to fail closed rather than hang trying
    # to reach a network this sandbox blocks.
    #
    # HOME is a per-service subdirectory (already provisioned for every DEK
    # service the same way), not the bare /var/empty: dek-qa's real Hermes
    # runtime state lives at /var/empty/.hermes (root-only, 0700). qa/tests
    # imports Hermes code that checks $HOME/.hermes/.env at import time; a
    # bare HOME=/var/empty collides with that real path and PermissionErrors
    # on dek-qa's private data instead of cleanly finding nothing there.
    #
    # `uv export` (unlike `uv pip compile`) needs an actual Python 3.11
    # interpreter present to run against -- hermes-agent's own project
    # pins 3.11 -- not just cached package metadata. uv's normal discovery
    # is its own managed install under ~/.local/share/uv/python, i.e.
    # /root again; vendored a copy to /opt/dek-vendor/python3.11 and put
    # its bin/ on PATH so uv's PATH-based fallback discovery finds it.
    completed = _run_status(arguments, cwd=cwd, env=env, timeout=timeout)
    if completed.returncode: raise BundleError(completed.stderr.decode("utf-8", "replace").strip() or "fixed command failed")
    return completed.stdout


def _run_status(arguments, *, cwd: Path | None = None, env=None, timeout=120):
    """Like _run(), but returns the completed process instead of raising on
    a nonzero exit -- for callers where a nonzero exit is an expected,
    meaningful outcome (e.g. `git merge-base --is-ancestor`), not a failure.
    """
    safe_env = {"HOME":"/var/empty/dek-builder", "PATH":"/opt/dek-vendor/python3.11/bin:/opt/dek-vendor/bin:/usr/bin:/bin", "UV_CACHE_DIR":"/opt/dek-vendor/uv-cache", "UV_OFFLINE":"1", "LANG":"C.UTF-8", "LC_ALL":"C.UTF-8", "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_SYSTEM":"/dev/null", "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_ATTR_NOSYSTEM":"1", "GIT_TERMINAL_PROMPT":"0", "GIT_ASKPASS":"/bin/false", "SSH_ASKPASS":"/bin/false"}
    if env:
        safe_env.update({key:value for key,value in env.items() if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_") or key=="GIT_CONFIG_COUNT" or key in PROXY_ENV_KEYS})
    return subprocess.run(arguments, cwd=cwd, env=safe_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def _digest(path: Path) -> str:
    value=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(65536), b""): value.update(chunk)
    return value.hexdigest()


def _canonical(value: dict) -> bytes:
    return b"dek-approved-bundle-v2\0" + json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _canonical_release(value: dict) -> bytes:
    return b"dek-final-release-v1\0" + json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _canonical_activation_ready(value: dict) -> bytes:
    return b"dek-activation-ready-v1\0" + json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _overwrite_in_place(path: Path, payload: bytes) -> None:
    """Overwrite an existing file's bytes without ever truncating it first.

    Unlike Path.write_text()/write_bytes() (open(..., "w") truncates to zero
    bytes as part of the open() call, before a single byte of new content is
    written), this keeps the previous valid content on disk for the entire
    duration of the write and only trims any leftover tail after the new
    bytes are confirmed durable. A crash before the write completes leaves
    the original content intact and re-readable, instead of an empty or
    half-written file. Deliberately does not create, rename, or chmod the
    path: some callers (release.json) must preserve the existing inode's
    owner/mode exactly, which only the file's actual owner may change.
    """
    descriptor = os.open(path, os.O_WRONLY)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(f"short write: {written} of {len(payload)} bytes")
        os.fsync(descriptor)
        os.ftruncate(descriptor, len(payload))
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, payload: bytes, mode: int = 0o664) -> None:
    atomic_write_bytes(path, payload, mode=mode, prefix="." + path.name + ".")


def write_candidate_regular(root: Path, relative: str, payload: bytes) -> Path:
    """Write a candidate through pinned directory descriptors, rejecting links.

    The checkout is attacker-controlled Git content.  Checking only ``resolve()``
    permits an in-tree symlink to redirect a write, so every component is opened
    with ``O_NOFOLLOW`` and the final inode is verified before and after writing.
    """
    path = PurePosixPath(relative)
    if (path.is_absolute() or not path.parts or path.parts[0] != "wiki" or
            path.suffix.lower() != ".md" or any(part in {"", ".", ".."} for part in path.parts)):
        raise BundleError("candidate path is invalid")
    root = Path(root).resolve(strict=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in path.parts[:-1]:
            try:
                descriptor = os.open(part, directory_flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=descriptors[-1])
                descriptor = os.open(part, directory_flags, dir_fd=descriptors[-1])
            except OSError as exc:
                raise BundleError("candidate path contains symlink or unsafe component") from exc
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise BundleError("candidate path component is not a directory")
            descriptors.append(descriptor)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path.name, flags, 0o644, dir_fd=descriptors[-1])
        except OSError as exc:
            raise BundleError("candidate target is a symlink or unsafe file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise BundleError("candidate target is not an exact regular file")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise BundleError("candidate write failed")
                view = view[written:]
            os.fsync(descriptor)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise BundleError("candidate target changed during write")
        finally:
            os.close(descriptor)
        directory = descriptors[-1]
        os.fsync(directory)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    target = root.joinpath(*path.parts)
    if target.read_bytes() != payload:
        raise BundleError("candidate bytes changed after write")
    return target


def validate_systemd_credential(path: Path) -> None:
    descriptor=None
    try:
        descriptor=os.open(Path(path),os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); details=os.fstat(descriptor)
        # Real systemd LoadCredential delivery on this host is root:root 0440
        # (still only reachable through the unit's own private credentials
        # mount, not via DAC group membership) -- not the 0400 this
        # originally assumed, which had never been exercised against a real
        # systemd LoadCredential run before this pipeline's first live use.
        if not stat.S_ISREG(details.st_mode) or details.st_nlink!=1 or stat.S_IMODE(details.st_mode) not in (0o400,0o440): raise BundleError("unsafe systemd credential")
    except OSError as exc: raise BundleError("unsafe or unreadable systemd credential") from exc
    finally:
        if descriptor is not None: os.close(descriptor)

def _auth_env(origin: str, credential_file: Path) -> dict:
    """Pass fixed HTTPS credentials through Git's in-process header config; never run helpers."""
    line=credential_file.read_text(encoding="utf-8").strip(); parsed=urlsplit(line)
    target=urlsplit(origin)
    if parsed.scheme!="https" or parsed.hostname!=target.hostname or parsed.path!=target.path or parsed.username is None or parsed.password is None:
        raise BundleError("credential is not bound to fixed origin")
    token=base64.b64encode(f"{parsed.username}:{parsed.password}".encode()).decode()
    return {"GIT_CONFIG_COUNT":"2","GIT_CONFIG_KEY_0":"credential.helper","GIT_CONFIG_VALUE_0":"","GIT_CONFIG_KEY_1":f"http.{origin}.extraHeader","GIT_CONFIG_VALUE_1":f"Authorization: Basic {token}"}


PUBLISHER_COMMIT_NAME = "DEK Publisher"
PUBLISHER_COMMIT_EMAIL = "publisher@invalid"


def _nearest_decision_commit(clone: Path, start: str) -> str | None:
    """Walk back from ``start`` to the nearest commit that is itself a
    previously published decision, skipping over anything else in between
    (an infra/code-fix commit, a doc update -- never a reviewed decision).

    activator.py's activation-ordering check requires a candidate's
    parent_commit to exactly equal the currently active generation's
    commit. parent_commit used to be the review's raw pinned
    snapshot_commit, which drifts forward on every unrelated commit to the
    branch, so any such commit landing between two real decisions
    permanently blocked the later one from ever activating -- dek-activator
    is deliberately credential-less and has no git access of its own to
    tell "skipped a decision" apart from "an infra commit landed in
    between", so this has to be resolved here, where real git history is
    available, not there.

    A commit counts as a decision only if it matches both the publisher's
    fixed commit author AND its "publish: " message prefix -- either alone
    is spoofable by an unrelated commit; both together are not, since only
    this method ever writes that combination.
    """
    output = _run((*GIT, "log", "--format=%H%x09%ae%x09%s", start), cwd=clone).decode("utf-8", "replace")
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, author_email, subject = parts
        if author_email == PUBLISHER_COMMIT_EMAIL and subject.startswith("publish: "):
            return sha
    return None


class ReleasePublisher:
    """Uses a new private clone for each decision; never accepts a live work tree."""
    def __init__(self, fixed_origin: str, signing_key: Ed25519PrivateKey, credential_file: Path, *, test_only_local_origin: bool = False):
        local = test_only_local_origin and Path(fixed_origin).is_absolute()
        if not local and (not fixed_origin.startswith("https://") or not fixed_origin.endswith("/dek.git")): raise BundleError("fixed HTTPS origin required")
        self.origin, self.signing_key, self.credential_file = fixed_origin, signing_key, Path(credential_file)
        self.test_only_local_origin = local

    def _clone_remote(self, destination: Path) -> None:
        if getattr(self, "test_only_local_origin", False):
            _run((*GIT, "-c", "protocol.file.allow=always", "clone", "--no-local", "--no-hardlinks", "--", self.origin, str(destination)))
        else:
            _run((*GIT, "clone", "--no-local", "--no-hardlinks", "--", self.origin, str(destination)),
                 env={**_auth_env(self.origin, self.credential_file), **_proxy_env()})

    def _push_remote(self, clone: Path, commit: str) -> None:
        local = getattr(self, "test_only_local_origin", False)
        push_args = (*GIT, "-c", "protocol.file.allow=always", "push", "--", self.origin, f"{commit}:refs/heads/main") if local \
            else (*GIT, "push", "--", self.origin, f"{commit}:refs/heads/main")
        env = None if local else {**_auth_env(self.origin, self.credential_file), **_proxy_env()}
        try:
            _run(push_args, cwd=clone, env=env)
            return
        except BundleError:
            pass
        # The push failed. If `commit` is already reachable from origin/main's
        # current tip -- published by an earlier call to this exact method
        # (publish() is meant to be idempotent), then superseded there by
        # later, unrelated pushes since -- that failure is benign: there is
        # nothing left to publish. Only a real conflict (this commit is NOT
        # an ancestor of the current remote tip) is a real error.
        fetch_args = (*GIT, "-c", "protocol.file.allow=always", "fetch", "--", self.origin, "main") if local \
            else (*GIT, "fetch", "--", self.origin, "main")
        _run(fetch_args, cwd=clone, env=env)
        remote_head = _run((*GIT, "rev-parse", "FETCH_HEAD"), cwd=clone).decode().strip()
        if remote_head == commit or _run_status((*GIT, "merge-base", "--is-ancestor", commit, remote_head), cwd=clone, env=env).returncode == 0:
            return
        raise BundleError(f"push rejected and {commit} is not an ancestor of remote main ({remote_head})")

    def prepare(self, output: Path, *, decision_id: str, nonce: str, commit: str) -> dict:
        approval_generation(decision_id, nonce)
        if not self.test_only_local_origin:
            validate_systemd_credential(self.credential_file)
        output.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="dek-publisher-clone-") as temporary:
            clone=Path(temporary)/"clone"
            self._clone_remote(clone)
            exact=_run((*GIT,"rev-parse",f"{commit}^{{commit}}"),cwd=clone).decode().strip()
            tree=_run((*GIT,"rev-parse",f"{exact}^{{tree}}"),cwd=clone).decode().strip()
            bundle=output/"repository.bundle"
            _run((*GIT,"branch","--force","dek-approved",exact),cwd=clone)
            _run((*GIT,"bundle","create",str(bundle),"refs/heads/dek-approved"),cwd=clone)
        approval={"schema_version":2,"decision_id":decision_id,"nonce":nonce,"origin":self.origin,"commit":exact,"tree":tree,"bundle_sha256":_digest(bundle)}
        (output/"approval.json").write_text(json.dumps(approval,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        (output/"approval.sig").write_bytes(self.signing_key.sign(_canonical(approval)))
        return approval

    def prepare_change(self, output: Path, decision: dict) -> dict:
        """Apply one approved review decision in its own clone and bundle that commit."""
        if decision.get("action") != "approve": raise BundleError("only approved decisions produce releases")
        decision_id=str(decision.get("decision_id","")); nonce=decision_id
        approval_generation(decision_id, nonce)
        if not self.test_only_local_origin:
            validate_systemd_credential(self.credential_file)
        output.mkdir(parents=True,exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="dek-publisher-clone-") as temporary:
            clone=Path(temporary)/"clone"
            self._clone_remote(clone)
            snapshot=str(decision.get("snapshot_commit",""))
            exact_snapshot=_run((*GIT,"rev-parse",f"{snapshot}^{{commit}}"),cwd=clone).decode().strip()
            snapshot_tree=_run((*GIT,"rev-parse",f"{exact_snapshot}^{{tree}}"),cwd=clone).decode().strip()
            if exact_snapshot!=snapshot or snapshot_tree!=decision.get("snapshot_tree"): raise BundleError("review snapshot identity mismatch")
            parent_commit=_nearest_decision_commit(clone,exact_snapshot)
            rough=clone/str(decision.get("rough_path","")); wiki=clone/str(decision.get("wiki_path",""))
            if not rough.resolve().is_relative_to((clone/"ingestion/rough").resolve()) or not wiki.resolve().is_relative_to((clone/"wiki").resolve()): raise BundleError("decision path escapes repository")
            candidate_bytes=decision["candidate_markdown"].encode("utf-8")
            # Checked against the clone's freshly-cloned tip -- i.e. what is
            # actually live right now -- before `reset --hard` below moves the
            # working tree back to this decision's own, possibly older, pinned
            # snapshot. Two decisions reviewed close together can each suggest
            # the same "next free number" wiki_path against their own snapshot
            # and never see each other's pick; without this check the second
            # one to publish silently clobbers the first one's unrelated,
            # already-published content.
            if wiki.is_file() and wiki.read_bytes()!=candidate_bytes:
                raise BundleError("wiki_path already published with different content")
            _run((*GIT,"reset","--hard",exact_snapshot),cwd=clone)
            try:
                rough_details=rough.lstat()
            except OSError as exc:
                raise BundleError("rough source is unreadable") from exc
            if not stat.S_ISREG(rough_details.st_mode) or rough_details.st_nlink!=1:
                raise BundleError("rough source is not an exact regular file")
            raw=rough.read_bytes()
            if "sha256:"+hashlib.sha256(raw).hexdigest()!=decision.get("rough_sha256"): raise BundleError("rough binding changed")
            wiki=write_candidate_regular(clone,str(decision.get("wiki_path","")),candidate_bytes)
            text=raw.decode("utf-8")
            text=text.replace("status: pending_review","status: promoted",1)
            # ingestion/automation/audit.py's lifecycle check fails closed on
            # any status: promoted rough whose wiki_target is still blank; a
            # rough's wiki_target is only ever prefilled by ingestion as a
            # suggestion, so the actual approved wiki_path must be recorded
            # here or the very next builder run permanently rejects this commit.
            text=re.sub(r"(?m)^wiki_target:.*$",f"wiki_target: {decision.get('wiki_path','')}",text,count=1)
            rough.write_text(text,encoding="utf-8")
            relative=(rough.relative_to(clone).as_posix(),wiki.relative_to(clone).as_posix())
            _run((*GIT,"add","--",*relative),cwd=clone)
            _run((*GIT,"-c",f"user.name={PUBLISHER_COMMIT_NAME}","-c",f"user.email={PUBLISHER_COMMIT_EMAIL}","commit","-m",f"publish: {decision_id}"),cwd=clone)
            exact=_run((*GIT,"rev-parse","HEAD^{commit}"),cwd=clone).decode().strip(); tree=_run((*GIT,"rev-parse","HEAD^{tree}"),cwd=clone).decode().strip()
            tree_entry=_run((*GIT,"ls-tree","HEAD","--",wiki.relative_to(clone).as_posix()),cwd=clone).decode("utf-8","strict").strip()
            committed=_run((*GIT,"show",f"HEAD:{wiki.relative_to(clone).as_posix()}"),cwd=clone)
            if not re.match(r"^100(?:644|755) blob [0-9a-f]{40,64}\t",tree_entry) or committed!=candidate_bytes:
                raise BundleError("candidate Markdown was not committed as the exact regular file")
            bundle=output/"repository.bundle"
            _run((*GIT,"branch","--force","dek-approved",exact),cwd=clone)
            _run((*GIT,"bundle","create",str(bundle),"refs/heads/dek-approved"),cwd=clone)
        decision_digest=hashlib.sha256(json.dumps(decision,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        approval={"schema_version":2,"decision_id":decision_id,"decision_sha256":decision_digest,"nonce":nonce,"origin":self.origin,"commit":exact,"tree":tree,"bundle_sha256":_digest(bundle)}
        if parent_commit is not None:
            approval["parent_commit"]=parent_commit
        (output/"approval.json").write_text(json.dumps(approval,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        (output/"approval.sig").write_bytes(self.signing_key.sign(_canonical(approval)))
        return approval

    @staticmethod
    def verify_review_snapshot(bundle: Path, commit: str, tree: str, expected_digest: str) -> None:
        if _digest(Path(bundle)) != expected_digest:
            raise BundleError("review snapshot bundle digest mismatch")
        with tempfile.TemporaryDirectory(prefix="dek-review-snapshot-") as temporary:
            clone=Path(temporary)/"clone"
            _run((*GIT,"-c","protocol.file.allow=always","clone","--no-checkout","--",str(bundle),str(clone)))
            exact=_run((*GIT,"rev-parse",f"{commit}^{{commit}}"),cwd=clone).decode().strip()
            actual_tree=_run((*GIT,"rev-parse",f"{exact}^{{tree}}"),cwd=clone).decode().strip()
            if exact != commit or actual_tree != tree:
                raise BundleError("review snapshot bundle object mismatch")

    @staticmethod
    def _complete_static_inventory(root: Path) -> dict[str,str]:
        digests={}
        for path in sorted(root.rglob("*")):
            if path.is_dir(): continue
            details=path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink!=1: raise BundleError("unsafe artifact entry")
            relative=path.relative_to(root).as_posix()
            if relative in {"approval.json","approval.sig","release.json","release.sig","activation-ready.json","activation-ready.sig","repository.bundle","release.lock","build-release.json"}: continue
            if relative!="dek-kb.json" and (not relative.startswith("site/") or path.suffix.lower() not in STATIC_SUFFIXES):
                raise BundleError("server-side executable content in release")
            digests[relative]=_digest(path)
        return digests

    def finalize(self, package: Path) -> dict:
        approval=json.loads((package/"approval.json").read_text(encoding="utf-8"))
        try: self.signing_key.public_key().verify((package/"approval.sig").read_bytes(),_canonical(approval))
        except Exception as exc: raise BundleError("prepared approval signature invalid") from exc
        claimed=json.loads((package/"release.json").read_text(encoding="utf-8"))
        generation=approval_generation(approval.get("decision_id"), approval.get("nonce"))
        if claimed.get("generation")!=generation or any(claimed.get(key)!=value for key,value in approval.items()):
            raise BundleError("built release is not approval-bound")
        actual=self._complete_static_inventory(package)
        if claimed.get("artifacts")!=actual: raise BundleError("built artifacts differ from release bytes")
        final={**approval,"generation":generation,"artifacts":actual}
        # Fail-closed DAC contract (DAC_MATRIX build-to-activator / publisher-signs-build):
        # release.json is owned by dek-builder and its activator-readable 0664 mode is the
        # builder's contract, set once by builder_entrypoint.make_activator_readable() before
        # the atomic rename.  Overwriting it here (write_text truncates in place) does NOT
        # change its mode, and the publisher -- not the owner and with an empty capability
        # set -- MUST NOT chmod it: that would raise EPERM (no CAP_FOWNER) and block every
        # finalize.  release.sig is created here and owned by the publisher, so pinning it to
        # 0664 is both allowed and required.  Never touch directory setgid semantics here.
        release_json=package/"release.json"
        _overwrite_in_place(release_json, (json.dumps(final,sort_keys=True,separators=(",",":"))+"\n").encode())
        release_sig=package/"release.sig"
        release_sig.write_bytes(self.signing_key.sign(_canonical_release(final)))
        os.chmod(release_sig,0o664)
        return final

    def publish(self, package: Path, *, queue_snapshot: dict | None = None) -> dict:
        """Idempotently push a fixed-build-gated signed package.

        process_decision() calls this unconditionally on every publisher run
        for every decision still in the approved queue, by design -- there is
        no separate "already published" memory anywhere else. This used to
        unconditionally delete any existing gate before attempting a fresh
        push; if that push then failed (non-fast-forward, because
        origin/main had moved on for any reason -- another decision, an
        unrelated commit), the gate was gone and never recreated, permanently
        stranding an already fully, successfully published decision at the
        activation step. Idempotency now lives in _push_remote() itself
        (a rejected push whose commit is provably already an ancestor of
        origin/main's current tip is treated as success, not failure) --
        the gate is always safely recreated with a *fresh* queue_snapshot
        afterward, deliberately not gated on comparing it to whatever was
        there before: a stale queue_snapshot on an unchanged release would
        never pass dek-activator's own freshness check anyway, so keeping
        an old one around would just relearn this exact failure mode.
        """
        if not (package/"release.json").is_file() or not (package/"release.sig").is_file():
            raise BundleError("final release build gate missing")
        approval=json.loads((package/"approval.json").read_text(encoding="utf-8"))
        try: self.signing_key.public_key().verify((package/"approval.sig").read_bytes(),_canonical(approval))
        except Exception as exc: raise BundleError("prepared approval signature invalid") from exc
        final=json.loads((package/"release.json").read_text(encoding="utf-8"))
        try: self.signing_key.public_key().verify((package/"release.sig").read_bytes(),_canonical_release(final))
        except Exception as exc: raise BundleError("final release signature invalid") from exc
        if any(final.get(key)!=value for key,value in approval.items()): raise BundleError("final release approval mismatch")
        bundle=package/"repository.bundle"
        if _digest(bundle)!=approval.get("bundle_sha256") or approval.get("origin")!=self.origin: raise BundleError("prepared bundle binding invalid")
        gate={"schema_version":1,"status":"pushed","generation":final["generation"],
              "commit":final["commit"],"tree":final["tree"],"bundle_sha256":final["bundle_sha256"],
              "release_sha256":_digest(package/"release.json"),
              "release_signature_sha256":_digest(package/"release.sig")}
        if queue_snapshot is not None:
            if set(queue_snapshot) != {"decision_queue_sha256", "decision_queue_size"}:
                raise BundleError("invalid decision queue snapshot")
            gate.update(queue_snapshot)
        if "parent_commit" in final:
            gate["parent_commit"] = final["parent_commit"]
        with tempfile.TemporaryDirectory(prefix="dek-publisher-push-") as temporary:
            clone=Path(temporary)/"clone"
            _run((*GIT,"-c","protocol.file.allow=always","clone","--no-checkout","--",str(bundle),str(clone)))
            commit=_run((*GIT,"rev-parse",f'{approval["commit"]}^{{commit}}'),cwd=clone).decode().strip()
            tree=_run((*GIT,"rev-parse",f'{commit}^{{tree}}'),cwd=clone).decode().strip()
            if commit!=approval["commit"] or tree!=approval["tree"]: raise BundleError("prepared Git identity mismatch")
            self._push_remote(clone,commit)
        # A prior or partial gate must never survive a new push attempt.  The
        # signature makes the eventual gate publisher-owned even though the
        # build directory is group writable by the builder.  Deliberately
        # deferred until here: only once a fresh push has actually succeeded,
        # never before, so a failed retry can never destroy a still-valid gate.
        for name in ("activation-ready.sig", "activation-ready.json"):
            (package/name).unlink(missing_ok=True)
        signature=self.signing_key.sign(_canonical_activation_ready(gate))
        _atomic_bytes(package/"activation-ready.json",json.dumps(gate,sort_keys=True,separators=(",",":")).encode()+b"\n")
        _atomic_bytes(package/"activation-ready.sig",signature)
        return approval


class BundleBuilder:
    FIXED_COMMANDS = (
        ("/usr/bin/python3","-m","unittest","discover","-s","ingestion/automation/tests","-v"),
        ("/var/lib/dek-qa/venv/bin/python","-m","unittest","discover","-s","web/tests","-v"),
        ("/var/lib/dek-qa/venv/bin/python","-m","unittest","discover","-s","qa/tests","-v"),
        ("/usr/bin/python3","-m","ingestion.automation.audit","--root","{snapshot}"),
        ("/var/lib/dek-qa/venv/bin/python","-m","web.site","--vault","{snapshot}","--output","{output}/site"),
        ("/var/lib/dek-qa/venv/bin/python","-m","qa.dek_qa.build_index","--vault","{snapshot}","--output","{output}/dek-kb.json"),
    )
    def __init__(self, approval_key: Ed25519PublicKey, runner=_run): self.approval_key, self.runner=approval_key, runner

    @staticmethod
    def validate_static_output(output: Path) -> dict[str,str]:
        required={"site/index.html","dek-kb.json"}; found=set(); digests={}
        for path in output.rglob("*"):
            details=path.lstat()
            if path.is_dir(): continue
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1: raise BundleError("unsafe build output")
            relative=path.relative_to(output).as_posix(); found.add(relative)
            if relative != "dek-kb.json" and (not relative.startswith("site/") or path.suffix.lower() not in STATIC_SUFFIXES): raise BundleError("server-side executable output forbidden")
            digests[relative]=_digest(path)
        if not required.issubset(found): raise BundleError("incomplete static output")
        return digests

    def build(self, package: Path, output: Path) -> dict:
        approval=json.loads((package/"approval.json").read_text(encoding="utf-8"))
        try: self.approval_key.verify((package/"approval.sig").read_bytes(),_canonical(approval))
        except Exception as exc: raise BundleError("approval signature invalid") from exc
        bundle=package/"repository.bundle"
        if _digest(bundle) != approval.get("bundle_sha256"): raise BundleError("bundle digest mismatch")
        output.mkdir(parents=True,exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="dek-builder-private-") as private:
            private_snapshot=Path(private)/"private_snapshot"
            _run((*GIT,"-c","protocol.file.allow=always","clone","--no-checkout","--",str(bundle),str(private_snapshot)))
            commit=_run((*GIT,"rev-parse",f'{approval["commit"]}^{{commit}}'),cwd=private_snapshot).decode().strip()
            tree=_run((*GIT,"rev-parse",f'{commit}^{{tree}}'),cwd=private_snapshot).decode().strip()
            if commit != approval["commit"] or tree != approval["tree"]: raise BundleError("bundle object identity mismatch")
            archive=_run((*GIT,"archive","--format=tar",commit),cwd=private_snapshot)
            import tarfile, io
            snapshot=Path(private)/"snapshot"; snapshot.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive),mode="r:") as tar:
                for member in tar.getmembers():
                    if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in Path(member.name).parts: raise BundleError("unsafe Git archive")
                tar.extractall(snapshot,filter="data")
            for command in self.FIXED_COMMANDS:
                expanded=tuple(part.format(snapshot=str(snapshot),output=str(output)) for part in command)
                self.runner(expanded,cwd=snapshot)
        artifacts=self.validate_static_output(output)
        generation=approval_generation(approval.get("decision_id"), approval.get("nonce"))
        metadata={**approval,"generation":generation,"artifacts":artifacts}
        shutil.copy2(package/"approval.json",output/"approval.json")
        shutil.copy2(package/"repository.bundle",output/"repository.bundle")
        (output/"release.json").write_text(json.dumps(metadata,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        shutil.copy2(package/"approval.sig",output/"approval.sig")
        return metadata
