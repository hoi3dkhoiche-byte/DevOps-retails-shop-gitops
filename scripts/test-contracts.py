#!/usr/bin/env python3
"""Run mandatory GitOps suites and retain native counts; empty/skipped suites fail."""
import argparse
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"release": ROOT / "scripts/release", "graph": ROOT / "tests"}


def run_suite(suite, report_path, stream=None):
    result = unittest.TextTestRunner(verbosity=2, stream=stream).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report = {"schema_version": 1, "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "successful": passed}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=tuple(SUITES))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    suite = unittest.TestLoader().discover(str(SUITES[args.suite]), pattern="test_*.py")
    if not run_suite(suite, args.report):
        raise SystemExit("Mandatory GitOps suite failed, had no tests, or skipped tests: " + args.suite)


if __name__ == "__main__":
    main()
