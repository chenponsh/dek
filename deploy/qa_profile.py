#!/usr/bin/python3
"""Migrate the protected QA profile without replacing it with a template."""
from __future__ import annotations

import argparse
import copy
import errno
import os
import secrets
import sys
import tempfile
from pathlib import Path

import yaml

# Run directly as `python3 -I .../deploy/qa_profile.py` in production (see
# PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from fsutil import atomic_write_bytes


MCP_ARGS = [
    "-m", "qa.dek_qa.mcp_server",
    "--active", "/var/lib/dek-activate/control/active.json",
    "--releases", "/var/lib/dek-activate/releases",
    "--generation-proof", "/run/dek-proofs/qa/active.json",
]

QA_READ_ONLY_TOOLS = frozenset({
    "mcp__dek_kb__dek_kb_search",
    "mcp__dek_kb__dek_kb_get",
    "mcp__dek_kb__dek_kb_recent",
})

PLATFORM_NAMES = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal",
    "mattermost", "matrix", "homeassistant", "email", "sms", "dingtalk",
    "api_server", "webhook", "msgraph_webhook", "feishu", "wecom",
    "wecom_callback", "weixin", "bluebubbles", "qqbot", "yuanbao", "relay",
})
PLATFORM_TOOL_KEYS = frozenset({
    "tools", "toolsets", "platform_toolsets", "enabled_tools", "enabled_toolsets",
    "mcp_servers", "tool_search", "known_plugin_toolsets",
})


def _required_mapping(value, key):
    child = value.get(key) if isinstance(value, dict) else None
    if not isinstance(child, dict):
        raise RuntimeError(f"production profile lacks {key}")
    return child


def _sanitize_platform_block(value: dict, *, enabled: bool) -> None:
    """Disable aliases and remove tool-routing keys without touching secrets."""
    value["enabled"] = enabled
    for key in tuple(value):
        if key in PLATFORM_TOOL_KEYS:
            del value[key]
            continue
        child = value[key]
        if isinstance(child, dict):
            _remove_nested_tool_controls(child)


def _remove_nested_tool_controls(value: dict) -> None:
    for key in tuple(value):
        if key in PLATFORM_TOOL_KEYS:
            del value[key]
        elif isinstance(value[key], dict):
            _remove_nested_tool_controls(value[key])


def migrate_profile(source: dict, python_executable: str) -> dict:
    """Preserve non-tool state while replacing the complete tool surface."""
    result = copy.deepcopy(source)
    if not isinstance(python_executable, str) or not Path(python_executable).is_absolute():
        raise RuntimeError("QA Python executable must be an absolute path")
    if result.get("group_sessions_per_user") is not True:
        raise RuntimeError("group session isolation must remain enabled")
    if not isinstance(result.get("session_reset"), dict) or not result["session_reset"]:
        raise RuntimeError("session reset constraints are missing")
    platforms = _required_mapping(result, "platforms")
    dingtalk = _required_mapping(platforms, "dingtalk")
    extra = _required_mapping(dingtalk, "extra")
    chats = extra.get("allowed_chats")
    if not isinstance(chats, list) or not chats or any(not isinstance(item, str) or not item for item in chats):
        raise RuntimeError("production allowed_chats must be nonempty")
    if extra.get("require_mention") is not True:
        raise RuntimeError("production require_mention must remain true")
    configured_platforms = set(platforms)
    gateway = result.get("gateway")
    if gateway is not None and not isinstance(gateway, dict):
        raise RuntimeError("production gateway configuration is invalid")
    gateway = gateway or {}
    gateway_platforms = gateway.get("platforms")
    if gateway_platforms is not None and not isinstance(gateway_platforms, dict):
        raise RuntimeError("production gateway platforms configuration is invalid")
    if isinstance(gateway_platforms, dict):
        configured_platforms.update(gateway_platforms)
    configured_platforms.update(name for name in PLATFORM_NAMES if isinstance(result.get(name), dict))
    configured_platforms.update(name for name in PLATFORM_NAMES if isinstance(gateway.get(name), dict))
    for name in PLATFORM_NAMES:
        if name != "dingtalk":
            platforms.setdefault(name, {})
            configured_platforms.add(name)

    for name, platform in platforms.items():
        if not isinstance(platform, dict):
            raise RuntimeError(f"production platform {name} is invalid")
        _sanitize_platform_block(platform, enabled=name == "dingtalk")
    for name in configured_platforms:
        enabled = name == "dingtalk"
        if name in PLATFORM_NAMES and isinstance(result.get(name), dict):
            _sanitize_platform_block(result[name], enabled=enabled)
        if name in PLATFORM_NAMES and isinstance(gateway.get(name), dict):
            _sanitize_platform_block(gateway[name], enabled=enabled)
        if isinstance(gateway_platforms, dict) and isinstance(gateway_platforms.get(name), dict):
            _sanitize_platform_block(gateway_platforms[name], enabled=enabled)
    dingtalk["enabled"] = True
    extra["allowed_users"] = ["*"]
    result["platform_toolsets"] = {"dingtalk": []}
    result["tools"] = {"tool_search": {"enabled": "off"}}
    result["multiplex_profiles"] = False
    result["profile_routes"] = []
    if gateway:
        gateway["multiplex_profiles"] = False
        gateway["profile_routes"] = []
        for key in PLATFORM_TOOL_KEYS:
            gateway.pop(key, None)
    result["mcp_servers"] = {"dek_kb": {
        "command": python_executable,
        "args": list(MCP_ARGS),
        "env": {"PYTHONPATH": "/opt/dek-qa/app"},
        "sampling": {"enabled": False},
    }}
    return result


def _atomic_yaml(path: Path, value: dict) -> None:
    payload = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode("utf-8")
    atomic_write_bytes(path, payload, mode=0o600, prefix=".config-", ensure_parent_mode=0o700)


def candidate_environment(profile_dir: Path, state_root: Path) -> dict[str, str]:
    """Create the complete writable Hermes runtime namespace below staging."""
    profile_dir = Path(profile_dir).resolve()
    state_root = Path(state_root).resolve()
    environment = {
        "HERMES_HOME": str(profile_dir),
        "HOME": str(state_root / "home"),
        "XDG_CACHE_HOME": str(state_root / "xdg-cache"),
        "XDG_CONFIG_HOME": str(state_root / "xdg-config"),
        "XDG_STATE_HOME": str(state_root / "xdg-state"),
        "TMPDIR": str(state_root / "tmp"),
    }
    for key, path in environment.items():
        if key != "HERMES_HOME":
            Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    return environment


def verify_write_confinement(paths: list[Path]) -> None:
    """Prove that candidate namespaces reject creation outside staging."""
    for root in paths:
        root = Path(root)
        if not root.is_dir():
            raise RuntimeError(f"write-confinement root is unavailable: {root}")
        probe = root / (".dek-a6-write-probe-" + secrets.token_hex(12))
        try:
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                raise RuntimeError(f"write-confinement probe failed unexpectedly: {root}") from exc
        else:
            os.close(descriptor)
            probe.unlink(missing_ok=True)
            raise RuntimeError(f"candidate can write outside staging: {root}")
    print("candidate write confinement probe passed")


def validate_with_hermes(profile_dir: Path, expected: dict, state_root: Path | None = None) -> None:
    """Exercise Hermes' parser, adapter, MCP discovery and final model surface."""
    state_root = Path(state_root or profile_dir / ".acceptance-state").resolve()
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = candidate_environment(profile_dir, state_root)
    previous = {key: os.environ.get(key) for key in environment}
    previous_tempdir = tempfile.tempdir
    os.environ.update(environment)
    tempfile.tempdir = environment["TMPDIR"]
    try:
        from gateway.config import Platform, load_gateway_config
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter
        from tools.mcp_tool import discover_mcp_tools, shutdown_mcp_servers
        loaded = load_gateway_config()
        config = loaded.platforms[Platform.DINGTALK]
        adapter = DingTalkAdapter(config)
        wanted = expected["platforms"]["dingtalk"]["extra"]
        if (not config.enabled or adapter._dingtalk_allowed_chats() != set(wanted["allowed_chats"])
                or not adapter._dingtalk_require_mention() or adapter._allowed_users != {"*"}
                or loaded.multiplex_profiles or loaded.profile_routes):
            raise RuntimeError("Hermes DingTalk adapter rejected migrated constraints")
        enabled_platforms = {
            platform.value for platform, platform_config in loaded.platforms.items()
            if platform_config.enabled
        }
        if enabled_platforms != {"dingtalk"}:
            raise RuntimeError(f"Hermes enabled unapproved QA platforms: {sorted(enabled_platforms)!r}")
        parsed = load_config()
        if parsed.get("platform_toolsets", {}).get("dingtalk") != []:
            raise RuntimeError("Hermes parser did not retain the empty DingTalk toolset")
        if str(parsed.get("tools", {}).get("tool_search", {}).get("enabled", "")).lower() != "off":
            raise RuntimeError("Hermes parser did not disable tool search")
        if set(parsed.get("mcp_servers", {})) != {"dek_kb"}:
            raise RuntimeError("Hermes parser found an unapproved MCP server")
        try:
            discovered = set(discover_mcp_tools())
            enabled = sorted(_get_platform_tools(parsed, "dingtalk"))
            final = {
                item.get("function", {}).get("name")
                for item in get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
            }
            if discovered != QA_READ_ONLY_TOOLS or final != QA_READ_ONLY_TOOLS:
                raise RuntimeError(
                    f"Hermes final QA tool surface is not exact: discovered={sorted(discovered)!r}, "
                    f"final={sorted(name for name in final if name)!r}"
                )
        finally:
            shutdown_mcp_servers()
    finally:
        tempfile.tempdir = previous_tempdir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_staged_with_hermes(profile_path: Path, runtime_python: str,
                                runtime_pythonpath: str, runtime_state_root: Path | None = None) -> None:
    """Validate a production profile through exact, staging-only path rebinding."""
    expected = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if not isinstance(expected, dict):
        raise RuntimeError("production profile is invalid")
    server = expected.get("mcp_servers", {}).get("dek_kb")
    if (not isinstance(server, dict)
            or server.get("env") != {"PYTHONPATH": "/opt/dek-qa/app"}
            or not isinstance(server.get("command"), str)
            or not server["command"].startswith("/var/lib/dek-qa/venvs/")
            or not server["command"].endswith("/bin/python")):
        raise RuntimeError("production MCP paths are not relocatable")
    if (not Path(runtime_python).is_absolute() or not Path(runtime_pythonpath).is_absolute()):
        raise RuntimeError("staged MCP paths must be absolute")
    staged = copy.deepcopy(expected)
    staged_server = staged["mcp_servers"]["dek_kb"]
    staged_server["command"] = runtime_python
    staged_server["env"] = {"PYTHONPATH": runtime_pythonpath}
    args = staged_server.get("args")
    if not isinstance(args, list):
        raise RuntimeError("production MCP arguments are invalid")
    occurrences = [index for index, value in enumerate(args) if value == "--generation-proof"]
    if occurrences:
        index = occurrences[0]
        if len(occurrences) != 1 or index + 1 >= len(args):
            raise RuntimeError("production generation proof path is ambiguous")
        state_root = Path(runtime_state_root or profile_path.parent / "acceptance-state")
        if not state_root.is_absolute():
            raise RuntimeError("staged state root must be absolute")
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        args[index + 1] = str(state_root / "generation-proof" / "active.json")
    else:
        state_root = Path(runtime_state_root or profile_path.parent / "acceptance-state")
    with tempfile.TemporaryDirectory(prefix=".qa-accept-", dir=profile_path.parent) as temporary:
        candidate = Path(temporary) / "config.yaml"
        _atomic_yaml(candidate, staged)
        if runtime_state_root is None:
            validate_with_hermes(candidate.parent, staged)
        else:
            validate_with_hermes(candidate.parent, staged, state_root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--python-executable")
    parser.add_argument("--validate-existing", type=Path)
    parser.add_argument("--runtime-python-executable")
    parser.add_argument("--runtime-pythonpath")
    parser.add_argument("--runtime-state-root", type=Path)
    parser.add_argument("--environment-file", type=Path)
    parser.add_argument("--forbid-write-root", action="append", type=Path, default=[])
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("QA profile validation requires Python 3.12")
    if args.validate_existing is not None:
        if any(value is not None for value in (args.source, args.target, args.python_executable)) or args.skip_validation:
            parser.error("--validate-existing cannot be combined with migration arguments")
        if args.environment_file is not None:
            try:
                from deploy.credential_gate import validate_process_environment
            except ImportError:
                from credential_gate import validate_process_environment
            validate_process_environment(args.environment_file, "qa")
        if args.forbid_write_root:
            verify_write_confinement(args.forbid_write_root)
        if (args.runtime_python_executable is None) != (args.runtime_pythonpath is None):
            parser.error("both staged runtime paths are required together")
        if args.runtime_state_root is not None and args.runtime_python_executable is None:
            parser.error("--runtime-state-root requires staged runtime paths")
        if args.runtime_python_executable is not None:
            validate_staged_with_hermes(
                args.validate_existing, args.runtime_python_executable, args.runtime_pythonpath,
                args.runtime_state_root,
            )
            return 0
        expected = yaml.safe_load(args.validate_existing.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            raise SystemExit("production profile is invalid")
        validate_with_hermes(args.validate_existing.parent, expected)
        return 0
    if args.environment_file is not None:
        parser.error("--environment-file is valid only with --validate-existing")
    if args.forbid_write_root:
        parser.error("--forbid-write-root is valid only with --validate-existing")
    if any(value is None for value in (args.source, args.target, args.python_executable)):
        parser.error("--source, --target and --python-executable are required for migration")
    source = yaml.safe_load(args.source.read_text(encoding="utf-8"))
    if not isinstance(source, dict): raise SystemExit("production profile is invalid")
    migrated = migrate_profile(source, args.python_executable)
    _atomic_yaml(args.target, migrated)
    if not args.skip_validation:
        validate_with_hermes(args.target.parent, migrated)
    return 0


if __name__ == "__main__": raise SystemExit(main())
