#!/usr/bin/python3
"""Fail-closed DNS, TLS, OAuth callback, identity and readiness checkpoint."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Run directly as `python3 -I .../deploy/readiness.py` in production (see
# PRODUCTION_ROLLOUT.md); -I suppresses Python's normal auto-add of the
# script's own directory to sys.path, so sibling-module imports need an
# explicit bootstrap rather than relying on that default.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from fsutil import atomic_write_bytes, read_bounded_regular


class ReadinessError(RuntimeError): pass


MARKER_TTL = timedelta(days=7)


def _contains_placeholder(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized=value.strip().lower()
    return ("[" in normalized and "]" in normalized) or any(
        token in normalized for token in ("changeme", "placeholder", "example.invalid")
    )


def _config_hmac(value: dict, secret: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ReadinessError("readiness HMAC key is invalid")
    canonical=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    return hmac.new(secret,b"dek-readiness-config-v1\0"+canonical,hashlib.sha256).hexdigest()


def _marker_hmac(marker: dict, secret: bytes) -> str:
    if not isinstance(secret,bytes) or len(secret)<32:
        raise ReadinessError("readiness HMAC key is invalid")
    canonical=json.dumps(marker,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    return hmac.new(secret,b"dek-readiness-marker-v1\0"+canonical,hashlib.sha256).hexdigest()


def _utc_now(clock=None) -> datetime:
    now=(clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(now,datetime) or now.tzinfo is None:
        raise ReadinessError("readiness clock must be timezone-aware")
    return now.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00","Z")


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value,str) or not value.endswith("Z"):
        raise ReadinessError(f"invalid {label} UTC timestamp")
    try: parsed=datetime.fromisoformat(value[:-1]+"+00:00")
    except ValueError as exc: raise ReadinessError(f"invalid {label} UTC timestamp") from exc
    if _utc_text(parsed)!=value: raise ReadinessError(f"invalid {label} UTC timestamp")
    return parsed


def marker_payload(value: dict, secret: bytes, *, clock=None,
                   confirmed_authorized_login: bool, confirmed_unauthorized_login: bool) -> dict:
    """Bind one human confirmation to the approved config and a UTC validity window.

    The two confirmed_* flags record that the operator personally exercised
    one allowlisted login (must succeed) and one non-allowlisted login (must
    be denied) before writing this marker -- the marker only asserts that
    confirmation happened, not when or by whom; that's a deliberate
    simplification given this project's public-content risk level.
    """
    if confirmed_authorized_login is not True or confirmed_unauthorized_login is not True:
        raise ReadinessError("both login checks must be confirmed")
    issued_at=_utc_now(clock)
    unsigned={"schema":1,"status":"verified","config_hmac":_config_hmac(value,secret),
              "issued_at":_utc_text(issued_at),"expires_at":_utc_text(issued_at+MARKER_TTL),
              "confirmed_authorized_login":True,"confirmed_unauthorized_login":True}
    return {**unsigned,"marker_hmac":_marker_hmac(unsigned,secret)}


def _write_marker_bytes(target: Path, payload: bytes) -> None:
    try:
        atomic_write_bytes(target, payload, mode=0o444, prefix=".ready-", ensure_parent_mode=0o777)
    except OSError as exc:
        raise ReadinessError("readiness marker write failed") from exc


def write_marker(target: Path, value: dict, secret: bytes, *, clock=None,
                 confirmed_authorized_login: bool, confirmed_unauthorized_login: bool) -> None:
    payload=marker_payload(value,secret,clock=clock,
                           confirmed_authorized_login=confirmed_authorized_login,
                           confirmed_unauthorized_login=confirmed_unauthorized_login)
    _write_marker_bytes(target,(json.dumps(payload,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8"))


def _secure_read(path: Path, *, mode: int, maximum: int = 65536) -> bytes:
    try:
        return read_bounded_regular(path, maximum=maximum, minimum=2, require_uid=0, require_mode=mode)
    except OSError as exc:
        raise ReadinessError("unsafe readiness file") from exc


def _read_json_marker(path: Path) -> tuple[dict,bytes]:
    raw=_secure_read(Path(path),mode=0o444)
    try: value=json.loads(raw)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise ReadinessError("corrupt readiness marker") from exc
    if not isinstance(value,dict): raise ReadinessError("corrupt readiness marker")
    return value,raw


def validate_marker(target: Path, value: dict, secret: bytes, *, clock=None) -> dict:
    """Fail closed unless the marker is authentic, config-bound, confirmed and unexpired."""
    validate_configuration(value)
    marker,_raw=_read_json_marker(Path(target))
    now=_utc_now(clock)
    expected_fields={"schema","status","config_hmac","issued_at","expires_at",
                     "confirmed_authorized_login","confirmed_unauthorized_login","marker_hmac"}
    if (set(marker)!=expected_fields
            or marker.get("schema")!=1 or marker.get("status")!="verified"
            or marker.get("config_hmac")!=_config_hmac(value,secret)
            or marker.get("confirmed_authorized_login") is not True
            or marker.get("confirmed_unauthorized_login") is not True):
        raise ReadinessError("invalid readiness marker")
    unsigned={key:item for key,item in marker.items() if key!="marker_hmac"}
    supplied=marker.get("marker_hmac")
    if (not isinstance(supplied,str) or re.fullmatch(r"[0-9a-f]{64}",supplied) is None
            or not hmac.compare_digest(supplied,_marker_hmac(unsigned,secret))):
        raise ReadinessError("invalid readiness marker authentication")
    issued_at=_parse_utc(marker.get("issued_at"),"readiness marker")
    expires_at=_parse_utc(marker.get("expires_at"),"readiness marker expiry")
    if expires_at-issued_at!=MARKER_TTL:
        raise ReadinessError("invalid readiness marker validity period")
    if issued_at>now:
        raise ReadinessError("readiness marker is from the future")
    if now>=expires_at:
        raise ReadinessError("readiness marker has expired")
    return marker


def validate_configuration(value: dict) -> tuple[str,int]:
    required={"review_origin","oauth_callback","reviewer_ids","expected_addresses","readiness_url"}
    if set(value)!=required: raise ReadinessError("unexpected readiness configuration")
    if _contains_placeholder(value): raise ReadinessError("placeholder readiness value")
    origin=urlsplit(value["review_origin"]); callback=urlsplit(value["oauth_callback"]); ready=urlsplit(value["readiness_url"])
    if origin.scheme!="https" or callback.scheme!="https" or ready.scheme!="https": raise ReadinessError("TLS is mandatory")
    callback_suffix = "/auth/callback"
    if not callback.path.endswith(callback_suffix):
        raise ReadinessError("OAuth callback/origin mismatch")
    prefix = callback.path[:-len(callback_suffix)]
    if (origin.path not in {"", "/"} or origin.query or origin.fragment
            or callback.netloc != origin.netloc or callback.query or callback.fragment
            or ready.netloc != origin.netloc or ready.query or ready.fragment
            or prefix.startswith("//") or ready.path != prefix + "/__ready"):
        raise ReadinessError("OAuth callback/origin mismatch")
    if not isinstance(value["reviewer_ids"],list) or not value["reviewer_ids"] or any(not isinstance(item,str) or not item.strip() for item in value["reviewer_ids"]): raise ReadinessError("reviewer IDs are empty")
    if not isinstance(value["expected_addresses"],list) or not value["expected_addresses"]: raise ReadinessError("expected DNS addresses are empty")
    return origin.hostname or "", origin.port or 443


def _pinned_readiness_payload(value: dict, host: str, port: int, addresses: list[str]) -> dict:
    ready = urlsplit(value["readiness_url"])
    context = ssl.create_default_context()
    last_error = None
    for address in sorted(addresses):
        raw = tls = connection = None
        try:
            raw = socket.create_connection((address, port), timeout=5)
            tls = context.wrap_socket(raw, server_hostname=host)
            raw = None  # the TLS socket now owns the connected socket
            peer = tls.getpeername()[0]
            if ipaddress.ip_address(peer) != ipaddress.ip_address(address):
                raise ReadinessError("review readiness peer address mismatch")
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.sock = tls
            tls = None  # HTTPConnection.close now owns the TLS socket
            connection.request(
                "GET",
                ready.path or "/",
                headers={"Host": ready.netloc, "Accept": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ReadinessError("review readiness redirect is forbidden")
            if response.status != 200:
                raise ReadinessError("review readiness response mismatch")
            try:
                return json.loads(response.read(4097))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReadinessError("review readiness response mismatch") from exc
        except ReadinessError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
            elif tls is not None:
                tls.close()
            elif raw is not None:
                raw.close()
    raise ReadinessError("review readiness connection failed") from last_error


def check(value: dict) -> None:
    host,port=validate_configuration(value)
    observed=sorted({str(item[4][0]) for item in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)})
    if observed!=sorted(value["expected_addresses"]): raise ReadinessError("DNS does not match the approved address set")
    payload=_pinned_readiness_payload(value,host,port,observed)
    if payload!={"status":"ready","oauth_callback":value["oauth_callback"]}: raise ReadinessError("review readiness response mismatch")


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",type=Path)
    parser.add_argument("--write-marker",type=Path)
    parser.add_argument("--marker",type=Path)
    parser.add_argument("--hmac-key",type=Path)
    parser.add_argument("--validate-marker",action="store_true")
    parser.add_argument("--configuration-only",action="store_true")
    parser.add_argument("--confirm-authorized-login",action="store_true")
    parser.add_argument("--confirm-unauthorized-login",action="store_true")
    args=parser.parse_args(argv)
    if args.validate_marker:
        if not args.config or not args.marker or not args.hmac_key or args.write_marker:
            raise SystemExit("invalid marker validation arguments")
        value=json.loads(args.config.read_text(encoding="utf-8"))
        secret=_secure_read(args.hmac_key,mode=0o400)
        validate_marker(args.marker,value,secret)
        return 0
    if args.config is None:
        raise SystemExit("--config is required")
    value=json.loads(args.config.read_text(encoding="utf-8"))
    if args.configuration_only: validate_configuration(value)
    else: check(value)
    if args.write_marker:
        if not args.hmac_key: raise SystemExit("--hmac-key is required with --write-marker")
        if not args.confirm_authorized_login or not args.confirm_unauthorized_login:
            raise SystemExit("--write-marker requires both login checks to be confirmed")
        write_marker(args.write_marker,value,_secure_read(args.hmac_key,mode=0o400),
                    confirmed_authorized_login=True,confirmed_unauthorized_login=True)
    return 0

if __name__ == "__main__": raise SystemExit(main())
