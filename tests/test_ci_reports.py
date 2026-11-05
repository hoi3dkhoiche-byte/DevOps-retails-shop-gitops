"""A green required check needs real executed tests, not empty or skipped discovery."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("test_contracts", Path(__file__).resolve().parents[1] / "scripts/test-contracts.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TestReports(unittest.TestCase):
    def run_case(self, functions):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            passed = runner.run_suite(unittest.TestSuite(unittest.FunctionTestCase(function) for function in functions),
                                      path, stream=io.StringIO())
            return passed, json.loads(path.read_text(encoding="utf-8"))

    def test_native_success_count_is_retained(self):
        passed, report = self.run_case([lambda: None])
        self.assertTrue(passed)
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["failures"], 0)

    def test_empty_discovery_is_not_a_passing_gate(self):
        passed, report = self.run_case([])
        self.assertFalse(passed)
        self.assertEqual(report["tests_run"], 0)

    def test_skipped_or_failing_tests_cannot_green_the_gate(self):
        def skipped():
            raise unittest.SkipTest("Missing required fixture")
        def failed():
            raise AssertionError("A contract failed")
        for function, field in ((skipped, "skipped"), (failed, "failures")):
            with self.subTest(field=field):
                passed, report = self.run_case([function])
                self.assertFalse(passed)
                self.assertEqual(report[field], 1)
