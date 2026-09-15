import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.performance
@pytest.mark.parametrize("scarce", [False, True])
def test_three_month_load_has_valid_result_or_clean_timeout_under_watchdog(scarce):
    args = [sys.executable, "-m", "tests.optimizer_benchmark"]
    if scarce:
        args.append("--scarce")
    child = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    report = json.loads(child.stdout)
    target = Path(".local/test-results") / f"wp05-load-{report['case']}.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    assert report["people"] == 100 and report["shifts"] == 200
    assert report["budget_seconds"] == 10
    assert report["result"]["status"] in ("optimal_primary", "feasible_timeout", "no_proposal")
    if report["result"]["status"] == "no_proposal":
        assert report["result"]["diagnostics"]["code"] == "time_limit_without_incumbent"
    assert report["result"]["diagnostics"]["partner_trials"] <= 10_000
