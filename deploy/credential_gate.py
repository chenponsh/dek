#!/usr/bin/python3
"""Content-aware Stage A credential gate. Never emits credential values."""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# Run directly as `python3 -I .../deploy/credential_gate.py` in production
# (see PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from fsutil import read_bounded_regular


class CredentialError(RuntimeError):
    pass


_PLACEHOLDERS = (
    b"***", b"placeholder", b"changeme", b"change-me", b"replace-me",
    b"replace_me", b"example", b"your-secret", b"your_secret", b"sample",
)
_SECRET_NAME = re.compile(r"(?:SECRET|TOKEN|PASSWORD|AUDIT_KEY|API_KEY|PRIVATE_KEY)\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
# Different HTTP client libraries check different casing conventions for the
# proxy variables, so real EnvironmentFile= configs legitimately set both
# (see deploy/release_bundle.py's PROXY_ENV_KEYS, which passes through the
# same six names for git subprocess calls); these are the only lowercase
# names _environment() accepts.
_LOWERCASE_PROXY_ENV_NAME = re.compile(r"(?:http|https|no)_proxy\Z")
_QA_ENVIRONMENT_FIELDS = frozenset({
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
    "DINGTALK_AGENT_ID",
    "DINGTALK_ALLOWED_USERS",
    "DINGTALK_ALLOW_ALL_USERS",
    "DINGTALK_ALLOWED_CHATS",
    "DINGTALK_REQUIRE_MENTION",
    # Network routing only; Hermes does not use these to select providers,
    # adapters, platforms, MCP servers, or model-visible tools.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
})


def _read_regular(path: Path, maximum: int = 1024 * 1024) -> bytes:
    try:
        return read_bounded_regular(path, maximum=maximum)
    except OSError as exc:
        raise CredentialError("credential is unreadable or unsafe") from exc


def validate_secret_bytes(value: bytes, *, label: str, minimum: int = 32) -> None:
    """Reject weak content by bytes; errors intentionally contain labels only."""
    if not isinstance(value, bytes):
        raise CredentialError(f"{label} has invalid type")
    stripped = value.strip()
    lowered = value.lower()
    compact = re.sub(rb"\s+", b"", lowered)
    if (not stripped or len(value) < minimum
            or any(token in compact for token in _PLACEHOLDERS)
            or (compact.startswith((b"<", b"[")) and compact.endswith((b">", b"]")))):
        raise CredentialError(f"{label} content is missing, placeholder, or too short")
    counts = Counter(value)
    entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
    if len(counts) < 8 or entropy < 3.0:
        raise CredentialError(f"{label} content has insufficient entropy")


def _environment(path: Path, kind: str) -> dict[str, bytes]:
    raw = _read_regular(path)
    # EnvironmentFile= supports shell-like quoting, backslash escaping and
    # physical-line continuation, but it is not a shell file.  The gate must
    # validate the bytes the service receives, not a competing interpretation.
    # Accept only the metacharacter-free systemd subset whose value is passed
    # byte-for-byte; reject every construct that would require interpretation.
    if b"\x00" in raw or b"\r" in raw:
        raise CredentialError(f"{kind} environment syntax is invalid")
    values: dict[str, bytes] = {}
    for line in raw.split(b"\n"):
        if not line:
            continue
        if line.startswith(b"#"):
            continue
        if b"=" not in line:
            raise CredentialError(f"{kind} environment syntax is invalid")
        raw_name, value = line.split(b"=", 1)
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CredentialError(f"{kind} environment name is invalid") from exc
        if (not (_ENV_NAME.fullmatch(name) or _LOWERCASE_PROXY_ENV_NAME.fullmatch(name))
                or name in values):
            raise CredentialError(f"{kind} environment name is invalid or duplicated")
        if (not value or any(byte < 0x21 or byte > 0x7E for byte in value)
                or any(marker in value for marker in (b"\\", b"'", b'"'))):
            raise CredentialError(f"{kind} environment value syntax is not byte-exact")
        values[name] = value

    required = {
        "web": {"DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_AGENT_ID", "DEK_WEB_REDIRECT_URI", "DEK_WEB_CLAIM_SECRET"},
        "review": {
            "DEK_REVIEW_AUDIT_KEY", "DEK_WEB_CLAIM_SECRET", "DEK_REVIEWER_IDS",
            # Used by deploy/notify_entrypoint.py (dek-notify.service, same
            # identity and secrets file as dek-review) to push work
            # notifications for new pending reviews and stuck publishes.
            "DEK_REVIEW_DINGTALK_CLIENT_ID", "DEK_REVIEW_DINGTALK_CLIENT_SECRET", "DEK_REVIEW_DINGTALK_AGENT_ID",
        },
        # No agent ID: unlike web/app.py's DingTalk OAuth login flow (which
        # needs one for its enterprise-internal AgentId-scoped API calls),
        # Hermes's DingTalk bot adapter authenticates over a different
        # surface and never reads DINGTALK_AGENT_ID -- confirmed against the
        # real, currently-running dek-qa deployment, whose environment file
        # and Hermes profile YAML both omit it entirely.
        "qa": {
            "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET",
            "DINGTALK_ALLOWED_USERS", "DINGTALK_ALLOW_ALL_USERS",
        },
    }.get(kind)
    if required is None or not required <= set(values):
        raise CredentialError(f"{kind} environment is missing required fields")
    if kind == "qa":
        if not set(values) <= _QA_ENVIRONMENT_FIELDS:
            raise CredentialError("qa environment contains an unapproved field")
        if values["DINGTALK_ALLOWED_USERS"] != b"*":
            raise CredentialError("qa DingTalk authorization must delegate to application visibility")
        if values["DINGTALK_ALLOW_ALL_USERS"].lower() != b"true":
            raise CredentialError("qa DingTalk application visibility delegation is invalid")
    for name, value in values.items():
        if _SECRET_NAME.search(name):
            validate_secret_bytes(value, label=f"{kind} environment secret", minimum=16)
        if name.endswith("AGENT_ID") and not re.fullmatch(rb"[0-9]{3,32}", value):
            raise CredentialError(f"{kind} agent identifier has invalid format")
        if name.endswith("CLIENT_ID") and not re.fullmatch(rb"[A-Za-z0-9._-]{8,128}", value):
            raise CredentialError(f"{kind} client identifier has invalid format")
        if name.endswith(("REDIRECT_URI", "ORIGIN")):
            try: parsed = urlsplit(value.decode("ascii"))
            except UnicodeDecodeError as exc: raise CredentialError(f"{kind} URL has invalid format") from exc
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise CredentialError(f"{kind} URL has invalid format")
    return values


def validate_process_environment(path: Path, kind: str,
                                 environ: dict[str, str] | os._Environ = os.environ) -> None:
    """Prove gated file bytes equal the values received by this service process."""
    values = _environment(path, kind)
    for name, expected in values.items():
        actual = environ.get(name)
        try:
            actual_bytes = actual.encode("utf-8") if isinstance(actual, str) else None
        except UnicodeEncodeError as exc:
            raise CredentialError(f"{kind} service environment encoding is invalid") from exc
        if actual_bytes != expected:
            raise CredentialError(f"{kind} service environment differs from gated bytes")


def _git_credential(path: Path, origin: str) -> None:
    raw = _read_regular(path)
    if b"\n" in raw.strip() or b"\r" in raw.strip():
        raise CredentialError("Git credential must contain exactly one URL")
    try: parsed = urlsplit(raw.strip().decode("utf-8")); target = urlsplit(origin)
    except UnicodeDecodeError as exc: raise CredentialError("Git credential encoding is invalid") from exc
    try:
        ports_match = parsed.port == target.port
    except ValueError:
        raise CredentialError("Git credential URL has invalid format") from None
    if (parsed.scheme != "https" or parsed.hostname != target.hostname or not ports_match
            or parsed.path != target.path or parsed.query or parsed.fragment
            or parsed.username is None or parsed.password is None):
        raise CredentialError("Git credential is not bound to the fixed origin")
    validate_secret_bytes(unquote(parsed.password).encode("utf-8"), label="Git password/token", minimum=16)
    if not unquote(parsed.username).strip():
        raise CredentialError("Git credential username is empty")


def _approval_key_pair(private_path: Path, public_path: Path) -> None:
    private_raw = _read_regular(private_path)
    public_raw = _read_regular(public_path)
    try:
        private = serialization.load_pem_private_key(private_raw, password=None)
        public = serialization.load_pem_public_key(public_raw)
        private_public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        public_bytes = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except (ValueError, TypeError) as exc:
        raise CredentialError("approval key format is invalid") from exc
    if not isinstance(private, Ed25519PrivateKey) or not isinstance(public, Ed25519PublicKey):
        raise CredentialError("approval keys must be Ed25519")
    if private_public != public_bytes:
        raise CredentialError("approval private/public keys do not match")


def _resolve_letsencrypt_symlink(path: Path) -> Path:
    """certbot's live/<domain>/*.pem are always symlinks into ../../archive/
    (root-owned, not attacker-writable); resolve that one expected hop so
    _read_regular's O_NOFOLLOW open lands on the real, final regular file
    rather than rejecting the standard Let's Encrypt layout outright."""
    try:
        return Path(path).resolve(strict=True)
    except OSError as exc:
        raise CredentialError("TLS credential path is unresolvable") from exc


def _tls_key_pair(certificate_path: Path, private_path: Path) -> None:
    try:
        certificate = x509.load_pem_x509_certificate(_read_regular(_resolve_letsencrypt_symlink(certificate_path)))
        private = serialization.load_pem_private_key(_read_regular(_resolve_letsencrypt_symlink(private_path)), password=None)
        certificate_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public = private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (ValueError, TypeError) as exc:
        raise CredentialError("TLS certificate/private key format is invalid") from exc
    if certificate_public != private_public:
        raise CredentialError("TLS certificate/private key do not match")


def validate_credential_set(*, git_credential: Path, fixed_origin: str,
                            approval_private: Path, approval_public: Path,
                            environments: dict[str, Path],
                            shared_secret_pairs: list[tuple[Path, Path]],
                            random_secrets: list[tuple[str, Path]],
                            tls_pair: tuple[Path, Path] | None = None) -> None:
    _git_credential(git_credential, fixed_origin)
    _approval_key_pair(approval_private, approval_public)
    for kind, path in environments.items():
        _environment(path, kind)
    for left, right in shared_secret_pairs:
        left_raw = _read_regular(left); right_raw = _read_regular(right)
        validate_secret_bytes(left_raw, label="shared secret", minimum=32)
        validate_secret_bytes(right_raw, label="shared secret", minimum=32)
        if left_raw != right_raw:
            raise CredentialError("shared secret copies do not match byte-for-byte")
    for label, path in random_secrets:
        validate_secret_bytes(_read_regular(path), label=label, minimum=32)
    if tls_pair is not None:
        _tls_key_pair(*tls_pair)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-credential", type=Path, required=True)
    parser.add_argument("--fixed-origin", required=True)
    parser.add_argument("--approval-private", type=Path, required=True)
    parser.add_argument("--approval-public", type=Path, required=True)
    parser.add_argument("--environment", action="append", default=[])
    parser.add_argument("--shared-secret", action="append", default=[])
    parser.add_argument("--random-secret", action="append", default=[])
    parser.add_argument("--tls-certificate", type=Path)
    parser.add_argument("--tls-private", type=Path)
    args = parser.parse_args(argv)
    try:
        environments = {kind: Path(path) for kind, path in (item.split(":", 1) for item in args.environment)}
        pairs = [(Path(left), Path(right)) for left, right in (item.split(":", 1) for item in args.shared_secret)]
        random_values = [(label, Path(path)) for label, path in (item.split(":", 1) for item in args.random_secret)]
        validate_credential_set(
            git_credential=args.git_credential, fixed_origin=args.fixed_origin,
            approval_private=args.approval_private, approval_public=args.approval_public,
            environments=environments, shared_secret_pairs=pairs, random_secrets=random_values,
            tls_pair=(args.tls_certificate, args.tls_private)
            if args.tls_certificate is not None and args.tls_private is not None else None,
        )
        if (args.tls_certificate is None) != (args.tls_private is None):
            raise CredentialError("TLS certificate and private key must be supplied together")
    except (CredentialError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
