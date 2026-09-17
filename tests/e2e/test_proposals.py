import json
import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_planning import capture_failure, login
from tests.e2e.test_surveys import in_test_app

pytestmark = pytest.mark.e2e


def test_proposal_preview_adoption_cancellation_and_failure_preserve_shared_draft():
    setup = json.loads(
        in_test_app("""
import json, uuid
from datetime import date, timedelta
from django.utils import timezone
from ephios.core.models import UserProfile
from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate
from ephios_shift_coordination.services import create_period
from ephios_shift_coordination.surveys import open_survey, save_response
from ephios_shift_coordination.drafts import load_plan, save_draft, validate_draft
coordinator = UserProfile.objects.get(email='demo-001@example.invalid')
period = create_period(coordinator, template_id=ServiceTemplate.objects.get(title='Dienst').pk,
    start_date=date(2035, 4, 1), end_date=date(2035, 4, 30), dates=[date(2035, 4, 2)],
    rules=PlanningSettings.objects.get(pk=1).snapshot(), creation_key=uuid.uuid4())
period = open_survey(coordinator, period.pk, expected_version=1)
for index in (5, 6, 23, 24):
    person = UserProfile.objects.get(email=f'demo-{index:03d}@example.invalid')
    response = period.responses.get(user=person)
    save_response(person, period.pk, expected_version=0, maximum=2, notes='Private fixture note',
        ratings={pk: 'preferred' for pk in response.offered_shifts.values_list('pk', flat=True)})
period.deadline = timezone.now() - timedelta(seconds=1)
period.save(update_fields=['deadline'])
uid = UserProfile.objects.get(email='demo-005@example.invalid').pk
sid = period.events.first().shifts.first().pk
plan = load_plan(coordinator, period.pk)
# One person alone leaves the two-person shift partly staffed, which is a confirmed exception.
checks = validate_draft(coordinator, period.pk, expected_version=plan['version'],
    fingerprint=plan['fingerprint'], assignments=[[uid, sid]])
confirmations = [{'token': v['token'], 'confirmed': True, 'reason': ''}
    for v in checks['violations']]
save_draft(coordinator, period.pk, expected_version=plan['version'],
    fingerprint=plan['fingerprint'], assignments=[[uid, sid]], confirmations=confirmations)
print(json.dumps({'period': period.pk, 'person': uid, 'shift': sid}))
""")
    )
    base = f"http://127.0.0.1:{os.environ['EPHIOS_HTTP_PORT']}"
    url = f"{base}/shift-coordination/planning/{setup['period']}/plan/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        first = browser.new_page(
            service_workers="block", locale="de-DE", viewport={"width": 1600, "height": 1000}
        )
        second = browser.new_page(service_workers="block", locale="de-DE")
        try:
            for page, index in ((first, 1), (second, 2)):
                login(page, f"demo-{index:03d}@example.invalid", "demo")
                page.goto(url)
            saved_choice = first.locator(
                f'input[data-person="{setup["person"]}"][data-shift="{setup["shift"]}"]'
            )
            expect(saved_choice).to_be_checked()
            saved_choice.uncheck()
            calculate = first.get_by_role("button", name="Vorschlag berechnen", exact=True)
            with first.expect_response(
                lambda response: response.url.endswith("/propose/"), timeout=45000
            ) as calculation:
                calculate.click()
            result = calculation.value.json()
            assert result["status"] == "optimal_primary"
            assert result["score"]["filled"] == 2
            assert len(result["assignments"]) == 4
            expect(first.locator("input[data-person]:checked")).to_have_count(0)
            second.reload()
            expect(second.locator("input[data-person]:checked")).to_have_count(1)
            assert "Private fixture note" not in json.dumps(result)
            adopt = first.get_by_role("button", name="Diesen Vorschlag übernehmen", exact=True)
            first.once("dialog", lambda dialog: dialog.dismiss())
            adopt.click()
            expect(first.locator("input[data-person]:checked")).to_have_count(0)
            first.once("dialog", lambda dialog: dialog.accept())
            adopt.click()
            expect(first.locator("input[data-person]:checked")).to_have_count(4)
            second.reload()
            expect(second.locator("input[data-person]:checked")).to_have_count(1)
            first.get_by_role("button", name="Gemeinsamen Entwurf speichern", exact=True).click()
            expect(first.locator("#draft-status")).to_have_text("Gemeinsamer Entwurf gespeichert")
            second.reload()
            expect(second.locator("input[data-person]:checked")).to_have_count(4)
            # A failed calculation and a clean no-proposal response both preserve local selections.
            one = first.locator("input[data-person]:checked").first
            one.uncheck()
            first.route(
                "**/propose/",
                lambda route: route.fulfill(
                    status=503, content_type="text/html", body="Unavailable"
                ),
            )
            calculate.click()
            expect(first.locator("#proposal-status")).to_contain_text("Anfrage ist fehlgeschlagen")
            expect(first.locator("input[data-person]:checked")).to_have_count(3)
            expect(adopt).to_be_disabled()
            first.unroute("**/propose/")
            first.route(
                "**/propose/",
                lambda route: route.fulfill(
                    json={
                        "status": "no_proposal",
                        "assignments": [],
                        "score": None,
                        "message": "Kein geprüfter Vorschlag verfügbar.",
                    }
                ),
            )
            calculate.click()
            expect(first.locator("#proposal-status")).to_have_text(
                "Kein geprüfter Vorschlag verfügbar."
            )
            expect(first.locator("input[data-person]:checked")).to_have_count(3)
            expect(adopt).to_be_disabled()
            first.unroute("**/propose/")
            # Recalculation remains a complete alternative despite local manual edits.
            with first.expect_response(
                lambda response: response.url.endswith("/propose/"), timeout=45000
            ) as recalculation:
                calculate.click()
            recalculated = recalculation.value.json()
            assert len(recalculated["assignments"]) == 4
            expect(first.locator("input[data-person]:checked")).to_have_count(3)
            first.screenshot(path=".local/test-results/planning-proposal.png", full_page=True)
            # An empty, validated timeout incumbent is adoptable, unlike no proposal.
            first.route(
                "**/propose/",
                lambda route: route.fulfill(
                    json={
                        **recalculated,
                        "status": "feasible_timeout",
                        "assignments": [],
                        "score": {"filled": 0, "yellow": 0, "preferred": 0, "partner_repeats": 0},
                        "message": "Zeitlimit erreicht. Der geprüfte Zwischenstand ist leer.",
                    }
                ),
            )
            calculate.click()
            expect(
                first.get_by_text(
                    "Mit den aktuellen Antworten lässt sich niemand einteilen.", exact=True
                )
            ).to_be_visible()
            expect(adopt).to_be_enabled()
            first.once("dialog", lambda dialog: dialog.accept())
            adopt.click()
            expect(first.locator("input[data-person]:checked")).to_have_count(0)
            second.reload()
            expect(second.locator("input[data-person]:checked")).to_have_count(4)
            assert (
                in_test_app(f"""
from ephios.core.models import LocalParticipation
print(LocalParticipation.objects.filter(shift__plannedshift__event__period_id={setup["period"]}).count())
""")
                == "0"
            )
        except Exception:
            capture_failure(first, "proposal-first-failure")
            capture_failure(second, "proposal-second-failure")
            raise
        finally:
            browser.close()
