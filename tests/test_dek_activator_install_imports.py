"""Regression test for a real production crash: deploy/activator_entrypoint.py
imports web.review.queue_lock, but SERVICES["dek-activator"] only ever
shipped deploy/, not web/. dek-activator.service's first real trigger (via
the review UI's publish button, after dek-builder and dek-review-publish
both succeeded) crashed with ModuleNotFoundError: No module named 'web'
the moment it tried to activate the built release.
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


class DekActivatorInstalledTreeImportsTests(unittest.TestCase):
    def test_installed_dek_activator_tree_imports_activator_entrypoint_standalone(self):
        """Build a real package from this checkout's actual deploy/ and web/
        files (only the prefixes SERVICES declares for dek-activator),
        install it the same way production does, then import
        deploy.activator_entrypoint in a subprocess with ONLY that
        installed tree on sys.path -- exactly how systemd invokes it
        (`python3 -I <installed>/deploy/activator_entrypoint.py`)."""
        prefixes = SERVICES["dek-activator"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            manifest_lines = []
            for prefix in prefixes:
                for source in sorted((REPO / prefix).rglob("*.py")):
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
                selected_services=["dek-activator"],
                test_only_allow_unsafe_ancestors={root.parent},
            )

            installed = roots["dek-activator"] / "app"
            self.assertTrue((installed / "deploy" / "activator_entrypoint.py").is_file())
            self.assertTrue((installed / "web" / "review.py").is_file())

            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import runpy; runpy.run_path('deploy/activator_entrypoint.py', run_name='not_main')"],
                cwd=installed, env={"PATH": "/usr/bin:/bin"},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
