#!/usr/bin/python3
"""One durable, recoverable transaction for the Stage A QA/ingestion cutover."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import posixpath
import shutil
import secrets
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Run directly as `python3 -I .../deploy/a6_cutover.py` in production (see
# PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from install_components import open_secure_journal
from fsutil import atomic_write_bytes, atomic_write_json, fsync_dir


JOURNAL_NAME = "a6-cutover.json"
FINALIZED_NAME = "a6-cutover.finalized.json"
RECOVERED_NAME = "a6-cutover.recovered.json"
UNIT_NAMES = (
    "dek-qa.service", "dek-source-ingest.service", "dek-source-ingest-proof.service",
    "dek-source-ingest.timer", "dek-source-ingest-alert@.service",
)
SERVICE_NAMES = UNIT_NAMES
UNIT_FILE_STATES = {
    "enabled", "enabled-runtime", "linked", "linked-runtime", "alias",
    "masked", "masked-runtime", "static", "disabled", "indirect",
    "generated", "transient", "bad",
}
FORWARD_UNSAFE_UNIT_FILE_STATES = {"linked", "linked-runtime", "generated", "transient", "bad"}


def _validated_fragment_path(fragment_path: str) -> str:
    """Return a canonical path that can be passed losslessly through execve."""
    if not isinstance(fragment_path, str) or not fragment_path or fragment_path == "/":
        raise RuntimeError("linked UnitFileState lacks a safe canonical absolute FragmentPath")
    if (not fragment_path.startswith("/") or fragment_path.startswith("//")
            or posixpath.normpath(fragment_path) != fragment_path
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in fragment_path)):
        raise RuntimeError("linked UnitFileState lacks a safe canonical absolute FragmentPath")
    try:
        utf8 = fragment_path.encode("utf-8", "strict")
        filesystem = os.fsencode(fragment_path)
        if (utf8.decode("utf-8", "strict") != fragment_path
                or os.fsdecode(filesystem) != fragment_path
                or b"\0" in filesystem):
            raise UnicodeError("non-round-tripping FragmentPath")
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("linked UnitFileState lacks a safe canonical absolute FragmentPath") from exc
    return fragment_path


def unit_file_restore_plan(unit: str, state: str, fragment_path: str) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """Return pre-activity and post-activity commands for one exact unit-file state."""
    if state == "bad":
        raise RuntimeError(f"bad UnitFileState cannot be restored for {unit}")
    if state in {"static", "indirect", "alias", "generated", "transient"}:
        return [], []
    reset = [
        ("unmask", unit),
        ("unmask", "--runtime", unit),
        ("disable", unit),
        ("disable", "--runtime", unit),
    ]
    if state == "enabled":
        return reset + [("enable", unit)], []
    if state == "enabled-runtime":
        return reset + [("enable", "--runtime", unit)], []
    if state == "disabled":
        return reset, []
    if state == "masked":
        return reset, [("mask", "--force", unit)]
    if state == "masked-runtime":
        return reset, [("mask", "--runtime", unit)]
    if state in {"linked", "linked-runtime"}:
        fragment_path = _validated_fragment_path(fragment_path)
        link = ("link", fragment_path) if state == "linked" else ("link", "--runtime", fragment_path)
        return reset + [link], []
    raise RuntimeError(f"unknown UnitFileState for {unit}: {state}")


def activity_restore_plan(active: str, sub: str) -> tuple[list[str], list[str], bool]:
    """Describe activity states exactly reconstructable with public systemctl commands."""
    if active == "failed" or sub == "failed":
        raise RuntimeError(
            f"cannot safely restore ActiveState/SubState {active}/{sub}: "
            "failed state cannot be reconstructed exactly with public systemctl commands"
        )
    if active == "active" and sub in {"running", "exited", "waiting", "elapsed", "listening", "active"}:
        return ["reset-failed"], ["start"], False
    if active == "inactive" and sub == "dead":
        return ["stop", "reset-failed"], [], False
    raise RuntimeError(f"cannot safely restore ActiveState/SubState {active}/{sub}")


def _fsync_dir(path: Path) -> None:
    fsync_dir(path)


def _atomic_json(path: Path, value: dict) -> None:
    atomic_write_json(path, value, mode=0o600, prefix=".a6-")


def _atomic_bytes(path: Path, value: bytes, mode: int) -> None:
    atomic_write_bytes(path, value, mode=mode, prefix=".a6-unit-")


def _entry(path: Path, anchor: str) -> list[dict]:
    if not path.exists() and not path.is_symlink():
        return [{"path": anchor, "type": "absent"}]
    details = path.lstat()
    common = {"path": anchor, "mode": stat.S_IMODE(details.st_mode), "uid": details.st_uid, "gid": details.st_gid}
    if path.is_symlink():
        return [{**common, "type": "symlink", "target": os.readlink(path)}]
    if path.is_file():
        return [{**common, "type": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    if not path.is_dir():
        raise RuntimeError("unsupported cutover filesystem object")
    result = [{**common, "type": "directory"}]
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        result.extend(_entry(child, anchor + "/" + child.name))
    return result


def tree_manifest(paths: list[Path]) -> list[dict]:
    result: list[dict] = []
    for index, path in enumerate(paths):
        result.extend(_entry(path, f"root-{index}"))
    return result


def _link_state(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if path.is_dir():
        return {"kind": "directory"}
    raise RuntimeError(f"unsafe live cutover path: {path}")


def _replace_link(path: Path, target: str) -> None:
    temporary = path.with_name("." + path.name + ".a6-new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _layout_id(layout: "CutoverLayout") -> str:
    identity = {
        "assets": [
            {key: str(value) for key, value in asset.items() if key in {"name", "candidate", "store", "link", "target"}}
            for asset in layout.assets()
        ],
        "unit_root": str(layout.unit_root),
        "units": sorted(layout.unit_candidates),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _completion_marker(value: dict, status: str) -> dict:
    return {
        "schema": 1, "status": status, "transaction_id": value["transaction_id"],
        "layout_id": value["layout_id"],
    }


def _read_completion(path: Path, layout: "CutoverLayout", status: str) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("invalid A6 completion marker")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid A6 completion marker") from exc
    if value != {
        "schema": 1, "status": status, "transaction_id": value.get("transaction_id"),
        "layout_id": _layout_id(layout),
    } or not isinstance(value.get("transaction_id"), str) or len(value["transaction_id"]) < 32:
        raise RuntimeError("invalid A6 completion marker")
    return value


@dataclass
class CutoverLayout:
    stage_venv: Path
    stage_profile: Path
    stage_source: Path
    live_root: Path
    unit_root: Path
    unit_candidates: dict[str, Path]
    journal_dir: Path
    test_only_allow_unsafe_ancestors: set[Path] = field(default_factory=set)
    custom_assets: list[dict] | None = None

    def assets(self) -> list[dict]:
        if self.custom_assets is not None:
            return self.custom_assets
        return [
            {"name": "venv", "candidate": self.stage_venv, "store": self.live_root / "venv-candidate",
             "link": self.live_root / "venv", "target": "venv-candidate"},
            {"name": "profile", "candidate": self.stage_profile, "store": self.live_root / "profile-candidate",
             "link": self.live_root / "profile", "target": "profile-candidate"},
            {"name": "source", "candidate": self.stage_source, "store": self.live_root / "source-candidate",
             "link": self.live_root / "source", "target": "source-candidate"},
        ]

    def manifest_paths(self) -> list[Path]:
        result = []
        for asset in self.assets():
            for key in ("candidate", "store", "link"):
                path = asset.get(key)
                if path is not None and Path(path) not in result:
                    result.append(Path(path))
        result.extend(self.unit_root / name for name in sorted(self.unit_candidates))
        return result


class MemoryServiceManager:
    def __init__(self, states: dict[str, tuple[bool, bool]]):
        self.states = {name: {"enabled": enabled, "active": active}
                       for name, (enabled, active) in states.items()}

    def contract(self) -> dict:
        return json.loads(json.dumps(self.states, sort_keys=True))

    def stop(self, name: str) -> None:
        self.states[name]["active"] = False

    def restore(self, contract: dict) -> None:
        self.states = json.loads(json.dumps(contract, sort_keys=True))

    def daemon_reload(self) -> None:
        return None

    def validate_apply(self, contract: dict) -> None:
        return None

    def validate_recover(self, contract: dict) -> None:
        return None


class SystemdServiceManager:
    def __init__(self, names=SERVICE_NAMES, *, runner=subprocess.run):
        self.names = tuple(names)
        self.runner = runner

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.runner(("/usr/bin/systemctl", *args), check=check, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def contract(self) -> dict:
        result = {}
        for name in self.names:
            output = self._run(
                "show", name, "--property=UnitFileState", "--property=ActiveState",
                "--property=SubState", "--property=FragmentPath",
            ).stdout
            properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            state = properties.get("UnitFileState", "")
            active = properties.get("ActiveState", "")
            sub = properties.get("SubState", "")
            fragment = properties.get("FragmentPath", "")
            if state not in UNIT_FILE_STATES:
                raise RuntimeError(f"unsupported UnitFileState for {name}: {state}")
            if not active or not sub:
                raise RuntimeError(f"incomplete systemd activity state for {name}")
            result[name] = {
                "unit_file_state": state, "active_state": active,
                "sub_state": sub, "fragment_path": fragment,
            }
        return result

    def validate_apply(self, contract: dict) -> None:
        for name, state in contract.items():
            unit_state = state["unit_file_state"]
            if unit_state in FORWARD_UNSAFE_UNIT_FILE_STATES:
                raise RuntimeError(f"cannot safely preserve UnitFileState {unit_state} for {name} during A6 apply")
            try:
                activity_restore_plan(state["active_state"], state["sub_state"])
            except RuntimeError as exc:
                raise RuntimeError(
                    f"cannot safely preserve systemd activity for {name} during A6 apply: "
                    f"ActiveState={state['active_state']}, SubState={state['sub_state']}"
                ) from exc

    def validate_recover(self, contract: dict) -> None:
        if not isinstance(contract, dict):
            raise RuntimeError(
                "Exact manual rollback required: A6 journal contains an "
                "unreconstructable systemd restore contract"
            )
        first_error = None
        for name, state in contract.items():
            values = state if isinstance(state, dict) else {}
            try:
                unit_file_restore_plan(
                    name, values.get("unit_file_state"), values.get("fragment_path"),
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                if first_error is None:
                    first_error = exc
            try:
                activity_restore_plan(values.get("active_state"), values.get("sub_state"))
            except (TypeError, ValueError, RuntimeError) as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError(
                "Exact manual rollback required: A6 journal contains an "
                "unreconstructable systemd restore contract"
            ) from first_error

    def stop(self, name: str) -> None:
        self._run("stop", name)

    def daemon_reload(self) -> None:
        self._run("daemon-reload")

    def restore(self, contract: dict) -> None:
        for name, state in contract.items():
            before, after = unit_file_restore_plan(
                name, state["unit_file_state"], state["fragment_path"],
            )
            for command in before:
                self._run(*command, check=False if command[0] in {"unmask", "disable"} else True)
            activity_before, activity_after, failed_start = activity_restore_plan(
                state["active_state"], state["sub_state"],
            )
            for command in activity_before:
                self._run(command, name, check=False if command == "stop" else True)
            for command in activity_after:
                self._run(command, name, check=not failed_start)
            for command in after:
                self._run(*command)
        observed = self.contract()
        if observed != contract:
            raise RuntimeError("systemd contract could not be restored exactly")


def _snapshot(layout: CutoverLayout, manager) -> dict:
    units = {}
    for name, candidate in sorted(layout.unit_candidates.items()):
        target = layout.unit_root / name
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(f"unsafe unit candidate: {name}")
        if target.exists() and not target.is_file() and not target.is_symlink():
            raise RuntimeError(f"unsafe installed unit: {name}")
        kind = "symlink" if target.is_symlink() else ("file" if target.exists() else "absent")
        units[name] = {
            "kind": kind,
            "mode": stat.S_IMODE(target.lstat().st_mode) if kind != "absent" else None,
            "content": base64.b64encode(target.read_bytes()).decode() if kind == "file" else None,
            "target": os.readlink(target) if kind == "symlink" else None,
            "candidate": str(candidate),
        }
    assets = []
    for asset in layout.assets():
        candidate = Path(asset["candidate"]) if asset.get("candidate") is not None else None
        store = Path(asset["store"])
        if candidate is not None:
            if not candidate.is_dir() or candidate.is_symlink() or store.exists():
                raise RuntimeError(f"unsafe or reused candidate: {asset['name']}")
        elif not store.is_dir() or store.is_symlink():
            raise RuntimeError(f"prepared component is unavailable: {asset['name']}")
        rendered = {key: (str(value) if isinstance(value, Path) else value) for key, value in asset.items()}
        rendered["before"] = _link_state(Path(asset["link"]))
        if rendered["before"]["kind"] == "directory":
            rendered["legacy"] = str(Path(asset["link"]).with_name(Path(asset["link"]).name + ".legacy-before-a6"))
        if rendered.get("target") == "$SOURCE_CURRENT_BEFORE":
            source_current = next((item for item in assets if item["name"] == "source-current"), None)
            if source_current is None or source_current["before"]["kind"] != "symlink":
                raise RuntimeError("source current link is unavailable for previous binding")
            rendered["target"] = source_current["before"]["target"]
        assets.append(rendered)
    paths = layout.manifest_paths()
    services_before = manager.contract()
    manager.validate_apply(services_before)
    return {"schema": 1, "status": "prepared", "transaction_id": secrets.token_hex(32),
            "layout_id": _layout_id(layout), "assets": assets, "units": units,
            "services_before": services_before, "manifest_paths": [str(path) for path in paths],
            "tree_before": tree_manifest(paths), "steps": [], "recovery_steps": []}


def _record(journal, value: dict, step: str) -> None:
    value["steps"].append(step)
    journal.atomic_json(JOURNAL_NAME, value)


def run_cutover(layout: CutoverLayout, manager, *, fail_after: str | None = None,
                fail_after_mutation: str | None = None) -> None:
    secured = open_secure_journal(
        layout.journal_dir,
        test_only_allow_unsafe_ancestors=layout.test_only_allow_unsafe_ancestors,
    )
    journal_path = secured.path / JOURNAL_NAME
    if journal_path.exists() or journal_path.is_symlink():
        secured.close()
        raise RuntimeError("unfinished A6 transaction; run recover")
    try:
        value = _snapshot(layout, manager)
    except BaseException:
        secured.close()
        raise
    for marker_name in (FINALIZED_NAME, RECOVERED_NAME):
        marker = secured.path / marker_name
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            secured.close()
            raise RuntimeError("invalid A6 completion marker")
        if marker.exists():
            secured.unlink(marker_name)
    secured.atomic_json(JOURNAL_NAME, value)

    def done(step: str) -> None:
        _record(secured, value, step)
        if fail_after == step:
            raise RuntimeError("injected cutover failure")

    for name in value["services_before"]:
        manager.stop(name); done("service-stop:" + name)
    for asset in value["assets"]:
        candidate = Path(asset["candidate"]) if asset.get("candidate") is not None else None
        store, link = Path(asset["store"]), Path(asset["link"])
        if candidate is not None:
            os.rename(candidate, store); _fsync_dir(candidate.parent); _fsync_dir(store.parent)
            done("asset-move:" + asset["name"])
        if asset["before"]["kind"] == "directory":
            legacy = Path(asset["legacy"])
            if legacy.exists() or legacy.is_symlink(): raise RuntimeError("legacy preservation target exists")
            os.rename(link, legacy); _fsync_dir(link.parent)
            if fail_after_mutation == "legacy-rename:" + asset["name"]:
                raise RuntimeError("injected cutover failure after mutation")
            done("legacy-rename:" + asset["name"])
        _replace_link(link, asset["target"]); done("link:" + asset["name"])
    for name, state in value["units"].items():
        candidate = Path(state["candidate"])
        _atomic_bytes(layout.unit_root / name, candidate.read_bytes(), 0o644)
        done("unit:" + name)
    manager.daemon_reload(); done("daemon-reload")
    manager.restore(value["services_before"])
    for name in ("dek-source-ingest.service", "dek-source-ingest.timer"):
        if name in value["services_before"]:
            manager.stop(name)
    done("service-contract")
    value["services_after"] = manager.contract()
    value["tree_after"] = tree_manifest([Path(path) for path in value["manifest_paths"]])
    value["links_after"] = {asset["name"]: _link_state(Path(asset["link"])) for asset in value["assets"]}
    value["status"] = "verified"
    secured.atomic_json(JOURNAL_NAME, value)
    if tree_manifest([Path(path) for path in value["manifest_paths"]]) != value["tree_after"]:
        raise RuntimeError("A6 post-cutover tree manifest changed")
    if manager.contract() != value["services_after"]:
        raise RuntimeError("A6 post-cutover service contract changed")
    secured.close()


def finalize(layout: CutoverLayout, manager, *, fail_after_mutation: str | None = None) -> None:
    """Forget recovery state only after the formal proof has been accepted."""
    secured = open_secure_journal(
        layout.journal_dir,
        test_only_allow_unsafe_ancestors=layout.test_only_allow_unsafe_ancestors,
    )
    journal = secured.path / JOURNAL_NAME
    if not journal.is_file() or journal.is_symlink():
        completed = _read_completion(secured.path / FINALIZED_NAME, layout, "finalized")
        secured.close()
        if completed is not None:
            return
        raise RuntimeError("no verified A6 transaction to finalize")
    value = json.loads(journal.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or value.get("status") != "verified":
        raise RuntimeError("A6 transaction is not verified")
    if value.get("layout_id") != _layout_id(layout):
        raise RuntimeError("A6 transaction layout mismatch")
    paths = [Path(path) for path in value["manifest_paths"]]
    if tree_manifest(paths) != value["tree_after"]:
        raise RuntimeError("A6 final tree manifest changed")
    if manager.contract() != value["services_after"]:
        raise RuntimeError("A6 final service contract changed")
    if {asset["name"]: _link_state(Path(asset["link"])) for asset in value["assets"]} != value["links_after"]:
        raise RuntimeError("A6 final link contract changed")
    marker = _completion_marker(value, "finalized")
    existing = _read_completion(secured.path / FINALIZED_NAME, layout, "finalized")
    if existing is not None and existing != marker:
        raise RuntimeError("A6 finalize marker transaction mismatch")
    if existing is None:
        secured.atomic_json(FINALIZED_NAME, marker)
    if fail_after_mutation == "completion-marker":
        raise RuntimeError("injected finalize failure after mutation")
    secured.unlink(JOURNAL_NAME)
    if fail_after_mutation == "journal-unlink":
        raise RuntimeError("injected finalize failure after mutation")
    secured.close()


def recover(layout: CutoverLayout, manager, *, fail_after_mutation: str | None = None) -> None:
    secured = open_secure_journal(
        layout.journal_dir,
        test_only_allow_unsafe_ancestors=layout.test_only_allow_unsafe_ancestors,
    )
    journal = secured.path / JOURNAL_NAME
    if not journal.is_file() or journal.is_symlink():
        completed = _read_completion(secured.path / RECOVERED_NAME, layout, "recovered")
        secured.close()
        if completed is not None:
            return
        raise RuntimeError("no recoverable A6 transaction")
    value = json.loads(journal.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or value.get("status") not in {"prepared", "verified"}:
        raise RuntimeError("invalid A6 transaction journal")
    if value.get("layout_id") != _layout_id(layout) or not isinstance(value.get("recovery_steps"), list):
        raise RuntimeError("invalid A6 transaction journal")
    manager.validate_recover(value.get("services_before"))

    def recovered_step(step: str, action, satisfied=None) -> None:
        already = step in value["recovery_steps"]
        if already and satisfied is not None and not satisfied():
            raise RuntimeError(f"recorded A6 recovery step diverged: {step}")
        if already:
            return
        if satisfied is None or not satisfied():
            action()
        if fail_after_mutation == step:
            raise RuntimeError("injected recovery failure after mutation")
        value["recovery_steps"].append(step)
        secured.atomic_json(JOURNAL_NAME, value)

    for name, state in value["units"].items():
        target = layout.unit_root / name
        original = base64.b64decode(state["content"], validate=True) if state["kind"] == "file" else None

        def unit_satisfied(target=target, state=state, original=original):
            if state["kind"] == "absent":
                return not target.exists() and not target.is_symlink()
            if state["kind"] == "symlink":
                return target.is_symlink() and os.readlink(target) == state["target"]
            return (target.is_file() and not target.is_symlink()
                    and stat.S_IMODE(target.stat().st_mode) == state["mode"]
                    and target.read_bytes() == original)

        def restore_unit(target=target, state=state, original=original):
            if state["kind"] == "absent":
                target.unlink(missing_ok=True); _fsync_dir(target.parent)
            elif state["kind"] == "symlink":
                if target.exists() and not target.is_symlink():
                    target.unlink()
                _replace_link(target, state["target"])
            else:
                _atomic_bytes(target, original, state["mode"])

        recovered_step("unit:" + name, restore_unit, unit_satisfied)
    for asset in reversed(value["assets"]):
        link, store = Path(asset["link"]), Path(asset["store"])
        candidate = Path(asset["candidate"]) if asset.get("candidate") is not None else None
        before = asset["before"]

        def link_satisfied(link=link, before=before, asset=asset):
            if before["kind"] == "absent":
                return not link.exists() and not link.is_symlink()
            if before["kind"] == "symlink":
                return link.is_symlink() and os.readlink(link) == before["target"]
            legacy = Path(asset["legacy"])
            return link.is_dir() and not link.is_symlink() and not legacy.exists() and not legacy.is_symlink()

        def restore_link(link=link, before=before, asset=asset):
            if link.is_symlink():
                link.unlink(); _fsync_dir(link.parent)
            elif link.exists() and before["kind"] != "directory":
                raise RuntimeError(f"unexpected recovery link object: {link}")
            if before["kind"] == "symlink":
                _replace_link(link, before["target"])
            elif before["kind"] == "directory":
                legacy = Path(asset["legacy"])
                if link.is_dir() and not link.is_symlink() and not legacy.exists():
                    return
                if not legacy.is_dir() or legacy.is_symlink() or link.exists() or link.is_symlink():
                    raise RuntimeError(f"legacy recovery state is ambiguous: {link}")
                os.rename(legacy, link); _fsync_dir(link.parent)

        recovered_step("link:" + asset["name"], restore_link, link_satisfied)
        if candidate is not None:
            def candidate_satisfied(candidate=candidate, store=store):
                return candidate.is_dir() and not candidate.is_symlink() and not store.exists() and not store.is_symlink()

            def restore_candidate(candidate=candidate, store=store):
                if candidate.exists() or candidate.is_symlink():
                    if store.exists() or store.is_symlink():
                        raise RuntimeError("candidate recovery state is ambiguous")
                    return
                if not store.is_dir() or store.is_symlink():
                    raise RuntimeError("candidate recovery source is unavailable")
                os.rename(store, candidate); _fsync_dir(store.parent); _fsync_dir(candidate.parent)

            recovered_step("candidate:" + asset["name"], restore_candidate, candidate_satisfied)
    recovered_step("daemon-reload", manager.daemon_reload)
    recovered_step("services", lambda: manager.restore(value["services_before"]),
                   lambda: manager.contract() == value["services_before"])
    paths = [Path(path) for path in value["manifest_paths"]]
    if tree_manifest(paths) != value["tree_before"]:
        raise RuntimeError("A6 recovery tree manifest mismatch")
    if manager.contract() != value["services_before"]:
        raise RuntimeError("A6 recovery service contract mismatch")
    secured.atomic_json(RECOVERED_NAME, _completion_marker(value, "recovered"))
    secured.unlink(JOURNAL_NAME); secured.close()


def _production_layout(args) -> CutoverLayout:
    digest = args.digest
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise SystemExit("invalid package digest")
    stage = args.stage_root
    live = Path("/var/lib/dek-qa")
    units = {name: args.package_root / "deploy" / "systemd" / name for name in UNIT_NAMES}
    source_store = Path("/opt/dek-source-ingest/versions") / digest
    assets = [
        {"name": "qa-venv", "candidate": stage / "qa" / "venv",
         "store": Path("/var/lib/dek-qa/venvs") / digest,
         "link": Path("/var/lib/dek-qa/venv"), "target": f"venvs/{digest}"},
        {"name": "qa-profile", "candidate": stage / "qa" / "profile",
         "store": Path("/var/lib/dek-qa/hermes/profile-versions") / digest,
         "link": Path("/var/lib/dek-qa/hermes/profiles/dek-qa"),
         "target": f"../profile-versions/{digest}"},
        {"name": "source-current", "candidate": None, "store": source_store,
         "link": Path("/opt/dek-source-ingest/current"), "target": f"versions/{digest}"},
        {"name": "source-previous", "candidate": None, "store": source_store,
         "link": Path("/opt/dek-source-ingest/previous"), "target": "$SOURCE_CURRENT_BEFORE"},
        {"name": "source-app", "candidate": None, "store": source_store,
         "link": Path("/opt/dek-source-ingest/app"), "target": "current"},
    ]
    return CutoverLayout(
        stage / "qa" / "venv", stage / "qa" / "profile", source_store,
        live, Path("/etc/systemd/system"), units, args.journal_dir, custom_assets=assets,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("apply", "recover", "finalize"))
    parser.add_argument("--digest", required=True)
    parser.add_argument("--stage-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--journal-dir", type=Path, default=Path("/var/lib/dek-install-transactions"))
    args = parser.parse_args(argv)
    layout = _production_layout(args); manager = SystemdServiceManager()
    if args.command == "apply": run_cutover(layout, manager)
    elif args.command == "recover": recover(layout, manager)
    else: finalize(layout, manager)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
