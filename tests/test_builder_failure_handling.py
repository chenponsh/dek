import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "deploy"))
from deploy import builder_entrypoint, release_bundle
from deploy.builder_entrypoint import MAX_BUILD_ATTEMPTS, process_packages


def completed(returncode, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


class FailureMessageTests(unittest.TestCase):
    def test_says_which_command_failed_and_shows_the_end_of_the_output(self):
        stderr = ("test_a ... ok\n" * 500 + "ERROR: test_b\nPermissionError: nope\nFAILED (errors=1)\n").encode()
        message = release_bundle._failure_message(("/usr/bin/python3", "-m", "unittest", "discover"), completed(1, b"", stderr))
        self.assertIn("/usr/bin/python3 -m unittest discover exited 1", message)
        self.assertTrue(message.rstrip().endswith("FAILED (errors=1)"))
        self.assertLess(len(message), 4000)

    def test_a_command_that_reports_on_stdout_is_not_reduced_to_nothing(self):
        message = release_bundle._failure_message(("python", "-m", "audit"), completed(2, b'{"missing_rough_events": 4}', b""))
        self.assertIn("missing_rough_events", message)

    def test_a_leftover_in_the_builder_home_is_named(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".hermes").mkdir()
            with patch.object(release_bundle, "BUILDER_HOME", home):
                message = release_bundle._failure_message(("t",), completed(1))
        self.assertIn(".hermes", message)
        self.assertIn("left it there", message)

    def test_an_empty_builder_home_adds_no_hint(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(release_bundle, "BUILDER_HOME", Path(temporary)):
                self.assertNotIn("hint", release_bundle._failure_message(("t",), completed(1)))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.approved, self.builds, self.failures = root / "approved", root / "builds", root / "failures"
        self.approved.mkdir()
        self.builds.mkdir()

    def package(self, name, decision):
        (self.approved / name).mkdir()
        (self.approved / name / "approval.json").write_text(json.dumps({"decision_id": decision, "nonce": decision}), encoding="utf-8")

    def test_a_package_that_keeps_failing_is_left_alone_after_the_limit(self):
        self.package("00-dead", "decision-dead-01")
        self.package("01-good", "decision-good-01")
        calls = []

        class Builder:
            def build(self, package, target):
                calls.append(package.name)
                if package.name == "00-dead":
                    raise RuntimeError("audit gate failed")
                target.mkdir()
                (target / "release.json").write_text("{}")

        for _ in range(MAX_BUILD_ATTEMPTS + 2):
            process_packages(Builder(), self.approved, self.builds, self.failures)
        self.assertEqual(calls.count("00-dead"), MAX_BUILD_ATTEMPTS)
        self.assertEqual(calls.count("01-good"), 1)          # built once, then it has a build
        record = json.loads(next(self.failures.glob("*.json")).read_text())
        self.assertEqual(record["attempts"], MAX_BUILD_ATTEMPTS)
        self.assertIn("audit gate failed", record["last_error"])

    def test_deleting_the_record_allows_another_try(self):
        self.package("00-dead", "decision-dead-01")
        calls = []

        class Builder:
            def build(self, package, target):
                calls.append(1)
                raise RuntimeError("no")

        for _ in range(MAX_BUILD_ATTEMPTS + 1):
            process_packages(Builder(), self.approved, self.builds, self.failures)
        self.assertEqual(len(calls), MAX_BUILD_ATTEMPTS)
        for record in self.failures.glob("*.json"):
            record.unlink()
        process_packages(Builder(), self.approved, self.builds, self.failures)
        self.assertEqual(len(calls), MAX_BUILD_ATTEMPTS + 1)

    def test_a_package_that_fails_once_and_then_builds_loses_its_failure_record(self):
        self.package("00-flaky", "decision-flaky-01")
        state = {"fail": True}

        class Builder:
            def build(self, package, target):
                if state["fail"]:
                    raise RuntimeError("environment problem")
                target.mkdir()
                (target / "release.json").write_text("{}")

        process_packages(Builder(), self.approved, self.builds, self.failures)
        self.assertEqual(len(list(self.failures.glob("*.json"))), 1)
        self.assertFalse((self.builds / "decision-flaky-01-decision-flaky-01").exists())
        state["fail"] = False                                   # the environment is fixed; the next run retries
        process_packages(Builder(), self.approved, self.builds, self.failures)
        self.assertTrue((self.builds / "decision-flaky-01-decision-flaky-01").is_dir())
        self.assertEqual(list(self.failures.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()


class PublisherPermanentFailureTests(unittest.TestCase):
    """An approval whose draft no longer exists is retried forever otherwise, with a traceback
    on every publish that looks like a fresh failure."""

    def setUp(self):
        from deploy.publisher_entrypoint import process_records
        self.process_records = process_records
        self.states = {}
        self.calls = []

    def write(self, key, value):
        self.states[key] = value

    def read(self, key):
        return self.states.get(key, {})

    def run_once(self, error):
        def worker(decision):
            self.calls.append(decision["decision_id"])
            raise error
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.process_records([{"decision_id": "dead-decision-01"}], worker, self.write, self.read)
        return err.getvalue()

    def test_a_missing_draft_is_marked_not_retryable_and_then_skipped_with_one_line(self):
        first = self.run_once(release_bundle.BundleError("rough source is unreadable"))
        self.assertIn("Traceback", first)
        self.assertIs(self.states["dead-decision-01"]["retryable"], False)
        self.assertIn("rough source is unreadable", self.states["dead-decision-01"]["last_error"])
        second = self.run_once(release_bundle.BundleError("rough source is unreadable"))
        self.assertEqual(self.calls, ["dead-decision-01"])             # the worker was not called again
        self.assertNotIn("Traceback", second)
        self.assertIn("cannot be retried", second)

    def test_a_transient_failure_stays_retryable_and_is_tried_again(self):
        self.run_once(release_bundle.BundleError("fixed command failed"))
        self.assertIs(self.states["dead-decision-01"]["retryable"], True)
        self.run_once(release_bundle.BundleError("fixed command failed"))
        self.assertEqual(len(self.calls), 2)

    def test_deleting_the_state_allows_another_try(self):
        self.run_once(release_bundle.BundleError("wiki_path already published with different content"))
        self.states.clear()
        self.run_once(release_bundle.BundleError("wiki_path already published with different content"))
        self.assertEqual(len(self.calls), 2)

    def test_without_a_state_reader_every_decision_is_still_tried(self):
        calls = []

        def worker(decision):
            calls.append(decision["decision_id"])
        self.process_records([{"decision_id": "a"}, {"decision_id": "b"}], worker, self.write)
        self.assertEqual(calls, ["a", "b"])
