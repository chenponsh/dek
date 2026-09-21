import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "deploy"))
import activator
import release_bundle


class StaticSuffixWhitelistTests(unittest.TestCase):
    def test_builder_and_activator_agree_and_allow_pdf(self):
        self.assertEqual(release_bundle.STATIC_SUFFIXES, activator.STATIC_SUFFIXES)
        self.assertIn(".pdf", activator.STATIC_SUFFIXES)

    def test_nothing_executable_is_allowed(self):
        for suffix in (".py", ".sh", ".php", ".exe", ".bat", ".cgi", ".pl", ".jsp", ".docx", ".zip", ".bin"):
            self.assertNotIn(suffix, activator.STATIC_SUFFIXES, suffix)


if __name__ == "__main__":
    unittest.main()
