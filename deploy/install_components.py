#!/usr/bin/python3
"""Crash-safe preparation and cutover of digest-addressed Stage A trees."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

# Run directly as `python3 -I .../deploy/install_components.py` in production
# (see PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from fsutil import atomic_write_bytes, atomic_write_json, fsync_dir

SERVICES = {
    "dek-web": ("web/",), "dek-qa": ("qa/",), "dek-review": ("web/", "deploy/"),
    "dek-publisher": ("deploy/", "web/"),
    "dek-source-ingest": ("deploy/", "ingestion/__init__.py", "ingestion/automation/"),
    "dek-builder": ("deploy/", "qa/", "web/", "ingestion/automation/"),
    "dek-activator": ("deploy/",),
}
DEFAULT_JOURNAL = Path("/var/lib/dek-install-transactions")


def _open_approved_root(path: Path, *, create: bool,
                        test_only_allow_unsafe_ancestors=frozenset(),
                        create_mode: int = 0o755, exact_final_mode: int | None = None) -> tuple[int, Path]:
    """Open/create an approved root without following any path-component link."""
    path = Path(path)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise RuntimeError("unsafe component root path")
    allowed = {Path(item) for item in test_only_allow_unsafe_ancestors}
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_details = os.fstat(descriptor)
        if (root_details.st_uid != 0 or stat.S_IMODE(root_details.st_mode) & 0o022):
            raise RuntimeError("unsafe component root ancestor ownership or mode")
        components = path.parts[1:]
        lexical = Path("/")
        for index, component in enumerate(components):
            final = index == len(components) - 1
            lexical /= component
            if final and create:
                try:
                    os.mkdir(component, create_mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise RuntimeError("unsafe component root path") from exc
            os.close(descriptor)
            descriptor = child
            details = os.fstat(descriptor)
            mode = stat.S_IMODE(details.st_mode)
            if not stat.S_ISDIR(details.st_mode) or details.st_uid != 0:
                raise RuntimeError("unsafe component root ancestor ownership or mode")
            if mode & 0o022 and lexical not in allowed:
                raise RuntimeError("unsafe component root ancestor ownership or mode")
            if final and exact_final_mode is not None and mode != exact_final_mode:
                raise RuntimeError("unsafe component root ownership or mode")
        return descriptor, path
    except Exception:
        os.close(descriptor)
        raise


def _fd_path(descriptor: int) -> Path:
    return Path(f"/proc/self/fd/{descriptor}")


def _open_child_directory(parent: int, name: str, *, create: bool, mode: int = 0o755) -> int:
    if not name or "/" in name or name in {".", ".."}:
        raise RuntimeError("unsafe component child directory")
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent,
        )
    except OSError as exc:
        raise RuntimeError("unsafe component child directory") from exc
    details = os.fstat(descriptor)
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022):
        os.close(descriptor)
        raise RuntimeError("unsafe component child ownership or mode")
    return descriptor


def _fsync_dir(path: Path) -> None:
    # Some callers pass a /proc/self/fd/N magic-symlink path, which O_NOFOLLOW
    # would refuse to open -- match the file's prior no-O_NOFOLLOW behavior.
    fsync_dir(path, no_follow=False)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""): value.update(chunk)
    return value.hexdigest()


def _manifest(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise RuntimeError("invalid install manifest")
        digest, relative = parts; relative = relative.strip(); candidate = Path(relative)
        if (not re.fullmatch(r"[0-9a-f]{64}", digest) or candidate.is_absolute()
                or ".." in candidate.parts or relative in result):
            raise RuntimeError("invalid install manifest")
        result[relative] = digest
    return result


class SecureJournal:
    """A journal namespace pinned by dirfd for its complete lifetime."""
    def __init__(self, descriptor: int, lexical: Path):
        self.descriptor = descriptor
        self.lexical = lexical
        self.path = _fd_path(descriptor)

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __del__(self):
        self.close()

    def atomic_json(self, name: str, value: dict) -> None:
        if "/" in name or name in {"", ".", ".."}:
            raise RuntimeError("unsafe journal entry")
        _atomic_json(self.path / name, value)

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.descriptor)
        os.fsync(self.descriptor)


def open_secure_journal(path: Path, *, test_only_allow_unsafe_ancestors=frozenset()) -> SecureJournal:
    if os.geteuid() != 0:
        raise RuntimeError("component transaction journal requires root")
    descriptor, lexical = _open_approved_root(
        Path(path), create=True, create_mode=0o700, exact_final_mode=0o700,
        test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
    )
    return SecureJournal(descriptor, lexical)


def _atomic_json(path: Path, value: dict) -> None:
    # Callers may pass a path under a /proc/self/fd/N pinned root.
    atomic_write_json(path, value, mode=0o600, prefix=".transaction-", parent_no_follow=False)


def _read_link_state(path: Path) -> list[str | None]:
    if path.is_symlink(): return ["symlink", os.readlink(path)]
    if not path.exists(): return ["absent", None]
    if path.name == "app" and (path.is_file() or path.is_dir()): return ["legacy", None]
    raise RuntimeError(f"unsafe pre-existing switch path: {path}")


def _file_state(path: Path) -> dict:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"unsafe pre-existing manifest path: {path}")
    if not path.exists(): return {"kind": "absent"}
    return {"kind": "file", "mode": stat.S_IMODE(path.stat().st_mode),
            "content": base64.b64encode(path.read_bytes()).decode("ascii")}


def _atomic_bytes(path: Path, content: bytes, mode: int) -> None:
    # Callers may pass a path under a /proc/self/fd/N pinned root.
    atomic_write_bytes(path, content, mode=mode, prefix="." + path.name + ".", parent_no_follow=False)


def _replace_link(path: Path, target: str | Path) -> None:
    temporary = path.with_name("." + path.name + ".new")
    if temporary.exists() or temporary.is_symlink(): temporary.unlink()
    temporary.symlink_to(target); os.replace(temporary, path); _fsync_dir(path.parent)


def _restore_link(root: Path, name: str, state: list[str | None]) -> None:
    path = root / name; kind, target = state
    if kind == "legacy":
        legacy = root / "legacy-app.before-versioned"
        legacy_preserved = legacy.exists() and not legacy.is_symlink()
        if path.exists() and not path.is_symlink() and not legacy_preserved:
            # The rename-to-legacy step never ran (crash before it, or this
            # recovery is replaying an already-completed restore): `path` is
            # already the exact pre-install state this branch exists to
            # reach. Nothing to overwrite; recognize it as satisfied rather
            # than treating an unmodified original as an unsafe surprise.
            return
        if path.exists() or path.is_symlink():
            if not path.is_symlink(): raise RuntimeError(f"refusing unsafe rollback overwrite: {path}")
            path.unlink(); _fsync_dir(root)
        if not legacy_preserved: raise RuntimeError("preserved legacy app is missing")
        os.rename(legacy, path); _fsync_dir(root); return
    if path.exists() or path.is_symlink():
        if not path.is_symlink(): raise RuntimeError(f"refusing unsafe rollback overwrite: {path}")
        path.unlink(); _fsync_dir(root)
    if kind == "symlink": _replace_link(path, str(target))


def _restore_file(path: Path, state: dict) -> None:
    if state["kind"] == "absent":
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f"refusing unsafe manifest rollback: {path}")
        if path.exists(): path.unlink(); _fsync_dir(path.parent)
        return
    _atomic_bytes(path, base64.b64decode(state["content"], validate=True), int(state["mode"]))


def _rollback_components(value: dict, pinned: dict[str, Path] | None = None, *,
                         test_only_allow_unsafe_ancestors=frozenset()) -> None:
    with contextlib.ExitStack() as opened:
        for service in reversed(value["order"]):
            component = value["components"][service]
            if pinned is None:
                descriptor, _ = _open_approved_root(
                    Path(component["root"]), create=False,
                    test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
                )
                opened.callback(os.close, descriptor)
                root = _fd_path(descriptor)
            else:
                root = pinned[service]
            for name in ("app", "current", "previous"):
                _restore_link(root, name, component["links"][name])
            for name in ("install-manifest.expected", "install-manifest.actual"):
                _restore_file(root / name, component["manifests"][name])


def _reconcile_pinned_journal(journal: SecureJournal,
                              *, test_only_allow_unsafe_ancestors=frozenset()) -> None:
    """Idempotently roll back any transaction not durably marked committed."""
    for name in sorted(value for value in os.listdir(journal.descriptor) if value.endswith(".json")):
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=journal.descriptor)
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except Exception:
            raise RuntimeError("invalid component transaction journal")
        if value.get("schema") != 1 or not isinstance(value.get("components"), dict):
            raise RuntimeError("invalid component transaction journal")
        if value.get("status") != "committed":
            _rollback_components(value, test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors)
        journal.unlink(name)


def reconcile_install_transactions(journal_dir: Path = DEFAULT_JOURNAL, *,
                                   test_only_allow_unsafe_ancestors=frozenset()) -> None:
    journal = open_secure_journal(
        journal_dir, test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
    )
    try:
        _reconcile_pinned_journal(
            journal, test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
        )
    finally:
        journal.close()


def _prepare_tree(package: Path, final: Path, staging: Path, expected: dict[str, str]) -> dict[str, str]:
    if staging.exists() or staging.is_symlink():
        if staging.is_dir() and not staging.is_symlink(): shutil.rmtree(staging)
        else: raise RuntimeError(f"unsafe stale staging tree: {staging}")
    if final.is_symlink() or (final.exists() and not final.is_dir()):
        raise RuntimeError("unsafe installed version tree")
    if not final.exists():
        staging.mkdir(mode=0o755)
        try:
            for relative in sorted(expected):
                source = package / relative
                if not source.is_file() or source.is_symlink() or _digest(source) != expected[relative]:
                    raise RuntimeError(f"package digest mismatch: {relative}")
                target = staging / relative; target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with source.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst); dst.flush(); os.fsync(dst.fileno())
                target.chmod(0o444)
            for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
                directory.chmod(0o555); _fsync_dir(directory)
            staging.chmod(0o555); _fsync_dir(staging); os.rename(staging, final); _fsync_dir(final.parent)
        except Exception:
            if staging.exists():
                staging.chmod(0o755)
                for directory in staging.rglob("*"):
                    if directory.is_dir(): directory.chmod(0o755)
                shutil.rmtree(staging)
            raise
    actual = {}
    for installed in final.rglob("*"):
        if installed.is_symlink() or not (installed.is_file() or installed.is_dir()):
            raise RuntimeError("unexpected installed path/type")
        if installed.is_file(): actual[installed.relative_to(final).as_posix()] = _digest(installed)
    if actual != expected: raise RuntimeError("installed manifest mismatch")
    return actual


def install_versioned_components(package: Path, manifest_path: Path, roots: dict[str, Path], digest: str,
                                   *, before_switch=None, journal_dir: Path = DEFAULT_JOURNAL,
                                   deferred_services=(), selected_services=None, after_step=None,
                                   test_only_allow_unsafe_ancestors=frozenset()) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or set(roots) != set(SERVICES):
        raise RuntimeError("invalid Stage A install inputs")
    deferred = set(deferred_services); selected = list(SERVICES if selected_services is None else selected_services)
    if not set(selected) <= set(SERVICES) or not deferred <= set(selected):
        raise RuntimeError("invalid component selection")
    journal = open_secure_journal(
        journal_dir, test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
    )
    _reconcile_pinned_journal(
        journal, test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
    )
    package = Path(package).resolve(strict=True); complete = _manifest(Path(manifest_path))
    with contextlib.ExitStack() as opened:
        opened.callback(journal.close)
        pinned_roots: dict[str, Path] = {}
        version_roots: dict[str, Path] = {}
        lexical_roots: dict[str, Path] = {}
        for service in selected:
            descriptor, lexical = _open_approved_root(
                Path(roots[service]), create=True,
                test_only_allow_unsafe_ancestors=test_only_allow_unsafe_ancestors,
            )
            opened.callback(os.close, descriptor)
            versions_descriptor = _open_child_directory(descriptor, "versions", create=True)
            opened.callback(os.close, versions_descriptor)
            pinned_roots[service] = _fd_path(descriptor)
            version_roots[service] = _fd_path(versions_descriptor)
            lexical_roots[service] = lexical

        prepared = {}
        for service in selected:
            root = pinned_roots[service]; versions = version_roots[service]; final = versions / digest
            expected = {rel: value for rel, value in complete.items() if rel.startswith(SERVICES[service])}
            if not expected: raise RuntimeError(f"{service}: empty install manifest")
            actual = _prepare_tree(package, final, versions / ("." + digest + ".installing"), expected)
            legacy = root / "legacy-app.before-versioned"
            if (root / "app").exists() and not (root / "app").is_symlink() and (legacy.exists() or legacy.is_symlink()):
                raise RuntimeError(f"{service}: legacy app preservation target already exists")
            prepared[service] = {
                # Journals retain only the approved lexical boundary. Live work
                # uses pinned_roots; crash recovery reopens this path no-follow.
                "root": str(lexical_roots[service]),
                "links": {name: _read_link_state(root / name) for name in ("current", "app", "previous")},
                "manifests": {name: _file_state(root / name) for name in ("install-manifest.expected", "install-manifest.actual")},
                "expected": "".join(f"{expected[p]}  {p}\n" for p in sorted(expected)),
                "actual": "".join(f"{actual[p]}  {p}\n" for p in sorted(actual)),
            }
        switching = [service for service in selected if service not in deferred]
        transaction = {"schema": 1, "status": "prepared", "digest": digest, "order": switching,
                       "components": {service: prepared[service] for service in switching}}
        journal_name = "install-components.json"; journal.atomic_json(journal_name, transaction)

        def completed(step: str) -> None:
            transaction["last_completed"] = step; journal.atomic_json(journal_name, transaction)
            if after_step is not None: after_step(step)

        try:
            for index, service in enumerate(switching):
                if before_switch is not None: before_switch(service, index)
                root = pinned_roots[service]
                _atomic_bytes(root / "install-manifest.expected", prepared[service]["expected"].encode(), 0o444); completed(f"{service}:manifest-expected")
                _atomic_bytes(root / "install-manifest.actual", prepared[service]["actual"].encode(), 0o444); completed(f"{service}:manifest-actual")
                current_state = prepared[service]["links"]["current"]
                if current_state[0] == "symlink": _replace_link(root / "previous", str(current_state[1])); completed(f"{service}:previous")
                _replace_link(root / "current", Path("versions") / digest); completed(f"{service}:current")
                app = root / "app"
                if prepared[service]["links"]["app"][0] == "legacy":
                    os.rename(app, root / "legacy-app.before-versioned"); _fsync_dir(root); completed(f"{service}:legacy-app")
                _replace_link(app, "current"); completed(f"{service}:app")
            for service in switching:
                root = pinned_roots[service]
                if os.readlink(root / "current") != f"versions/{digest}" or os.readlink(root / "app") != "current":
                    raise RuntimeError(f"{service}: post-switch verification failed")
            transaction["status"] = "committed"; journal.atomic_json(journal_name, transaction)
            if after_step is not None: after_step("committed")
            journal.unlink(journal_name)
        except Exception:
            _rollback_components(transaction, pinned_roots)
            journal.unlink(journal_name)
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True); parser.add_argument("--digest", required=True)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--cutover-source-ingest", action="store_true"); args = parser.parse_args(argv)
    roots = {name: Path("/opt") / name for name in SERVICES}
    selected, deferred = (["dek-source-ingest"], set()) if args.cutover_source_ingest else (None, {"dek-source-ingest"})
    install_versioned_components(args.package, args.manifest, roots, args.digest, journal_dir=args.journal_dir,
                                   deferred_services=deferred, selected_services=selected)
    return 0


if __name__ == "__main__": raise SystemExit(main())
