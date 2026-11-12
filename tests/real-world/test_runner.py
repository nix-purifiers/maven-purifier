# SPDX-License-Identifier: MIT
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("runner", Path(__file__).with_name("run-project.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ReportsTest(unittest.TestCase):
    def counts(self, xml):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "TEST-example.xml").write_text(xml)
            return runner.test_counts(directory)

    def test_container_totals_are_not_counted_twice(self):
        counts = self.counts('<testsuites><testsuite tests="12"><testsuite tests="12" '
                             'skipped="2" errors="1" failures="3"/></testsuite></testsuites>')
        self.assertEqual(counts, dict(reports=1, tests=12, executed=10, skipped=2,
                                      errors=1, failures=3))

    def test_all_skipped_does_not_prove_test_execution(self):
        self.assertEqual(self.counts('<testsuite tests="10" skipped="10"/>')["executed"], 0)

    def test_root_suite_with_children_counts_leaf_suites(self):
        self.assertEqual(self.counts('<testsuite tests="12"><testsuite tests="12"/></testsuite>')["tests"], 12)

    def test_empty_report_directory_has_no_executed_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(runner.test_counts(directory)["executed"], 0)

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            self.counts('<testsuite tests="-1"/>')


if __name__ == "__main__":
    unittest.main()
