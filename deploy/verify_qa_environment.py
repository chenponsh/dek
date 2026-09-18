#!/usr/bin/python3
"""Verify the exact Python 3.12 QA distribution and source summary."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""): value.update(chunk)
    return value.hexdigest()


def environment_summary(hermes_source: Path) -> dict:
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        direct = distribution.read_text("direct_url.json")
        packages[name] = {
            "version": distribution.version,
            "direct_url_sha256": hashlib.sha256(direct.encode()).hexdigest() if direct else None,
        }
    return {
        "schema": 1,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "packages": dict(sorted(packages.items())),
        "hermes_source_sha256": _digest(hermes_source),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--hermes-source", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12): raise SystemExit("QA runtime must use Python 3.12")
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    actual = environment_summary(args.hermes_source)
    if actual != expected: raise SystemExit("QA environment package/source summary mismatch")
    return 0


if __name__ == "__main__": raise SystemExit(main())
