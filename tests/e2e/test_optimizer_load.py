import json
import os
from pathlib import Path
from time import monotonic

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_planning import capture_failure, login
from tests.e2e.test_surveys import in_test_app

pytestmark = pytest.mark.e2e


def test_native_load_http_budget_queries_and_responsive_preview():
    fixture_code = Path("tests/e2e/optimizer_load_seed.py").read_text()
    period_id = None
    base = f"http://127.0.0.1:{os.environ['EPHIOS_HTTP_PORT']}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            service_workers="block", locale="de-DE", viewport={"width": 1600, "height": 1000}
        )
        try:
            login(page, "demo-001@example.invalid", "demo")
            for scarce in (False, True):
                report = json.loads(
                    in_test_app(fixture_code + f"\nload_fixture({period_id!r}, scarce={scarce!r})")
                )
                period_id = report["period"]
                page.goto(f"{base}/shift-coordination/planning/{period_id}/plan/", timeout=60000)
                expect(page.locator(".sc-day-box").first).to_be_visible()
                started = monotonic()
                with page.expect_response(
                    lambda response: response.url.endswith("/propose/"), timeout=60000
                ) as pending:
                    page.get_by_role("button", name="Vorschlag berechnen", exact=True).click()
                    # The calendar remains interactive while the server is computing.
                    expect(
                        page.get_by_role("button", name="Vorschlag berechnen", exact=True)
                    ).to_be_disabled()
                    page.locator("#people-filter").fill("Demoperson 007")
                    expect(page.locator("#planning-people .sc-person:not([hidden])")).to_have_count(
                        1
                    )
                    page.locator("#people-filter").fill("")
                report["http_seconds"] = monotonic() - started
                result = pending.value.json()
                report["result"] = result
                Path(f".local/test-results/wp05-http-{report['case']}.json").write_text(
                    json.dumps(report, indent=2)
                )
                assert pending.value.status == 200, result
                assert result["status"] in {"optimal_primary", "feasible_timeout"}, result
                assert report["people"] == 100 and report["shifts"] == 201
                assert result["diagnostics"]["elapsed_seconds"] < 12
                # Read queries are measured, with a ceiling to catch accidental per-rating queries.
                assert report["snapshot_queries"] < 4000
                assert result["score"]["filled"] > 0
                expect(page.locator("input[data-person]:checked")).to_have_count(0)
                expect(
                    page.get_by_role("button", name="Diesen Vorschlag übernehmen", exact=True)
                ).to_be_enabled()
                page.screenshot(path=f".local/test-results/planning-load-{report['case']}.png")
            assert (
                in_test_app(f"""
from ephios_shift_coordination.models import DraftAssignment
print(DraftAssignment.objects.filter(period_id={period_id}).count())
""")
                == "0"
            )
        except Exception:
            capture_failure(page, "optimizer-load-failure")
            raise
        finally:
            browser.close()
