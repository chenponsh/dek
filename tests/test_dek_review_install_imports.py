"""Regression test for a real production outage: web/review.py imports
`deploy.release_bundle`, but SERVICES["dek-review"] only shipped "web/" to
/opt/dek-review/app -- dek-review.service crash-looped in production with
`ModuleNotFoundError: No module named 'deploy'` the moment it was restarted
onto freshly-installed code, because nothing had ever actually installed
dek-review's component tree and then imported it the way the real systemd
unit does (PYTHONPATH=/opt/dek-review/app only, nothing else on sys.path).
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


class DekReviewInstalledTreeImportsTests(unittest.TestCase):
    def test_installed_dek_review_tree_imports_review_app_standalone(self):
        """Build a real package from this checkout's actual web/ and deploy/
        files (only the prefixes SERVICES declares for dek-review), install
        it the same way production does, then import web.review_app in a
        subprocess with ONLY that installed tree on sys.path -- exactly how
        systemd invokes it (PYTHONPATH=/opt/dek-review/app, no -I)."""
        prefixes = SERVICES["dek-review"]
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
                selected_services=["dek-review"],
                test_only_allow_unsafe_ancestors={root.parent},
            )

            installed = roots["dek-review"] / "app"
            self.assertTrue((installed / "web" / "review_app.py").is_file())

            result = subprocess.run(
                [sys.executable, "-c", "import web.review_app"],
                cwd=installed, env={"PYTHONPATH": str(installed), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
