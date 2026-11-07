# SPDX-License-Identifier: MIT
"""Require successful Gson test execution, rather than only a Maven exit code."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
reports = sorted(root.glob("**/target/surefire-reports/TEST-*.xml"))
assert reports, "Gson did not produce test reports"
executed = 0
for report in reports:
    suite = ET.parse(report).getroot()
    assert int(suite.get("failures", "0")) == 0, report
    assert int(suite.get("errors", "0")) == 0, report
    executed += int(suite.get("tests", "0")) - int(suite.get("skipped", "0"))
assert executed > 0, "Gson skipped every test"
assert (root / "gson/target/gson-2.15.0.jar").is_file(), "Gson library jar missing"
print(f"Gson executed {executed} tests across {len(reports)} reports")
