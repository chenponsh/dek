from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[2]
HERMES = Path(os.environ.get("DEK_QA_HERMES_SOURCE", "/opt/dek-qa/hermes-agent"))
LOCK = ROOT / "requirements-dek-qa.lock.txt"
DIRECT = ROOT / "requirements-dek-qa.txt"


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_versions(text: str) -> dict[str, str]:
    return {
        normalized(match.group(1)): match.group(2)
        for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==([^\s;\\]+)", text)
    }


def applicable_requirement_versions(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*;\s*(.*?))?\s*\\?$", line)
        if not match:
            continue
        marker = match.group(3)
        if marker and not Marker(marker).evaluate():
            continue
        result[normalized(match.group(1))] = match.group(2)
    return result


class CombinedDependencyLockTests(unittest.TestCase):
    def test_combined_lock_builder_is_path_independent(self) -> None:
        from qa.dek_qa.dependency_lock import build_combined_lock

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_lock = Path(first) / "lock.txt"
            second_lock = Path(second) / "lock.txt"
            build_combined_lock(HERMES, DIRECT, first_lock)
            build_combined_lock(HERMES, DIRECT, second_lock)
            self.assertEqual(first_lock.read_bytes(), second_lock.read_bytes())

    def test_combined_lock_contains_complete_hermes_core_mcp_export(self) -> None:
        exported = subprocess.run(
            [
                "uv", "export", "--project", str(HERMES), "--locked", "--no-dev",
                "--extra", "mcp", "--no-emit-project", "--format", "requirements.txt",
                "--no-hashes", "--quiet",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        expected = applicable_requirement_versions(exported)
        combined = requirement_versions(LOCK.read_text(encoding="utf-8"))
        self.assertEqual(expected, {name: combined.get(name) for name in expected})

    def test_constraint_generation_omits_ambiguous_lock_versions(self) -> None:
        from qa.dek_qa.dependency_lock import unique_registry_constraints

        lock = {"package": [
            {"name": "single", "version": "1.0", "source": {"registry": "x"}},
            {"name": "forked", "version": "1.0", "source": {"registry": "x"}},
            {"name": "forked", "version": "2.0", "source": {"registry": "x"}},
        ]}
        self.assertEqual(["single==1.0"], unique_registry_constraints(lock))

    def test_combined_lock_matches_hermes_locked_versions(self) -> None:
        hermes_lock = tomllib.loads((HERMES / "uv.lock").read_text(encoding="utf-8"))
        hermes_versions = {
            normalized(package["name"]): package["version"]
            for package in hermes_lock["package"]
            if package.get("version")
        }
        combined = requirement_versions(LOCK.read_text(encoding="utf-8"))
        mismatches = {
            name: (combined[name], version)
            for name, version in hermes_versions.items()
            if name in combined and combined[name] != version
        }
        self.assertEqual({}, mismatches)

    def test_all_qa_direct_requirements_are_in_combined_lock(self) -> None:
        direct = requirement_versions(DIRECT.read_text(encoding="utf-8"))
        combined = requirement_versions(LOCK.read_text(encoding="utf-8"))
        self.assertEqual(direct, {name: combined.get(name) for name in direct})

    def test_combined_lock_has_hashes_for_every_unconditional_requirement(self) -> None:
        blocks = re.split(r"(?m)(?=^[A-Za-z0-9_.-]+==)", LOCK.read_text(encoding="utf-8"))
        requirements = [block for block in blocks if re.match(r"^[A-Za-z0-9_.-]+==", block)]
        self.assertTrue(requirements)
        missing = [block.split("==", 1)[0] for block in requirements if "--hash=sha256:" not in block]
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
