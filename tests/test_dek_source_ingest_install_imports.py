"""Regression test for a real production failure: source_ingest_entrypoint.py's
load_ingestion_modules() explicitly spec-loads ingestion/__init__.py by file
path (deliberate hardening -- it never trusts sys.path against untrusted
cloned repo content), but ingestion/__init__.py was never shipped to
/opt/dek-source-ingest/app: SERVICES["dek-source-ingest"] only ever included
"ingestion/automation/", not the top-level ingestion/__init__.py the loader
requires to exist as a real file. dek-source-ingest-proof.service's first
real run failed with "candidate ingestion package is incomplete or unsafe"
the moment it tried to load the actually-installed package tree.
"""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy"))
sys.path.insert(0, str(REPO))
from deploy.install_components import SERVICES, install_versioned_components


class DekSourceIngestInstalledTreeLoadsIngestionModulesTests(unittest.TestCase):
    def test_installed_tree_satisfies_load_ingestion_modules(self):
        prefixes = SERVICES["dek-source-ingest"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            manifest_lines = []
            for prefix in prefixes:
                source_path = REPO / prefix
                sources = [source_path] if source_path.is_file() else sorted(source_path.rglob("*.py"))
                for source in sources:
                    relative = source.relative_to(REPO).as_posix()
                    target = package / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    manifest_lines.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {relative}\n")
            manifest = root / "PACKAGE.sha256"
            manifest.write_text("".join(manifest_lines), encoding="utf-8")

            roots = {name: root / name for name in SERVICES}
            install_versioned_components(
                package, manifest, roots, "a" * 64,
                journal_dir=root / "journal",
                selected_services=["dek-source-ingest"],
                test_only_allow_unsafe_ancestors={root.parent},
            )

            installed = roots["dek-source-ingest"] / "app"
            self.assertTrue((installed / "ingestion" / "__init__.py").is_file())
            self.assertTrue((installed / "ingestion" / "automation" / "cli.py").is_file())

            result = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, '.'); "
                 "from deploy.source_ingest_entrypoint import load_ingestion_modules; "
                 "load_ingestion_modules('.')"],
                cwd=installed, env={"PATH": "/usr/bin:/bin"},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
