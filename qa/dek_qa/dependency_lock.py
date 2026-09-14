from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def unique_registry_constraints(lock: dict) -> list[str]:
    versions: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    for package in lock.get("package", []):
        if not package.get("version") or not package.get("source", {}).get("registry"):
            continue
        key = normalized(package["name"])
        display.setdefault(key, package["name"])
        versions[key].add(package["version"])
    return sorted(
        f"{display[name]}=={next(iter(found))}"
        for name, found in versions.items()
        if len(found) == 1
    )


def build_combined_lock(hermes_project: Path, direct: Path, output: Path) -> None:
    """Build a path-independent Linux/Python 3.12 lock from fixed relative inputs."""
    with tempfile.TemporaryDirectory(prefix="dek-qa-lock-") as temporary:
        work = Path(temporary)
        constraints = work / "constraints.txt"
        hermes = work / "hermes.txt"
        direct_copy = work / "direct.txt"
        lock = tomllib.loads((hermes_project / "uv.lock").read_text(encoding="utf-8"))
        constraints.write_text(
            "\n".join(unique_registry_constraints(lock)) + "\n", encoding="utf-8"
        )
        shutil.copyfile(direct, direct_copy)
        subprocess.run(
            [
                "uv", "export", "--project", str(hermes_project), "--locked", "--no-dev",
                "--extra", "mcp", "--no-emit-project", "--format", "requirements.txt",
                "--no-hashes", "--output-file", "hermes.txt", "--quiet",
            ],
            cwd=work,
            check=True,
        )
        subprocess.run(
            [
                "uv", "pip", "compile", "hermes.txt", "direct.txt",
                "--constraints", "constraints.txt", "--python-version", "3.12",
                "--python-platform", "x86_64-unknown-linux-gnu", "--generate-hashes",
                "--custom-compile-command",
                "python3 -m qa.dek_qa.dependency_lock --build-combined-lock",
                "--output-file", str(output.resolve()), "--quiet",
            ],
            cwd=work,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate unambiguous constraints from Hermes uv.lock")
    parser.add_argument("--hermes-lock", type=Path)
    parser.add_argument("--hermes-project", type=Path, default=Path("/opt/dek-qa/hermes-agent"))
    parser.add_argument("--direct", type=Path, default=Path("requirements-dek-qa.txt"))
    parser.add_argument("--build-combined-lock", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.build_combined_lock:
        build_combined_lock(args.hermes_project, args.direct, args.output)
        return
    if args.hermes_lock is None:
        parser.error("--hermes-lock is required unless --build-combined-lock is used")
    lock = tomllib.loads(args.hermes_lock.read_text(encoding="utf-8"))
    args.output.write_text("\n".join(unique_registry_constraints(lock)) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
