import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy"))
import install_components


class PackageManifestCoversInstalledFiles(unittest.TestCase):
    """Every path a component installs must be in PACKAGE.sha256, or the
    installer leaves it out and the service fails at start-up (this happened
    with ingestion/__init__.py: 'candidate ingestion package is incomplete')."""

    def test_every_selected_file_and_tree_is_listed(self):
        listed = {line.split(None, 1)[1].strip() for line in (REPO / "deploy" / "PACKAGE.sha256").read_text(encoding="utf-8").splitlines() if line.strip()}
        for service, selections in install_components.SERVICES.items():
            for selection in selections:
                if selection.endswith("/"):
                    self.assertTrue(any(path.startswith(selection) for path in listed), f"{service}: nothing listed under {selection}")
                else:
                    self.assertIn(selection, listed, f"{service}: {selection} is not in PACKAGE.sha256")

    def test_ingest_loader_requirements_are_all_listed(self):
        source = (REPO / "deploy" / "source_ingest_entrypoint.py").read_text(encoding="utf-8")
        names = re.search(r'for name in \(([^)]*)\)\s*\n\s*\]', source)
        listed = (REPO / "deploy" / "PACKAGE.sha256").read_text(encoding="utf-8")
        self.assertIsNotNone(names)
        for name in re.findall(r'"(\w+)"', names.group(1)):
            self.assertIn(f"ingestion/automation/{name}.py", listed)


if __name__ == "__main__":
    unittest.main()
