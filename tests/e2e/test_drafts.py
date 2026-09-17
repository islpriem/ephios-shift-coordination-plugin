import json
import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_planning import capture_failure, login
from tests.e2e.test_surveys import in_test_app

pytestmark = pytest.mark.e2e


def test_two_coordinators_share_drafts_and_cannot_overwrite_stale_version():
    setup = json.loads(
        in_test_app("""
import json, uuid
from datetime import date, timedelta
from django.utils import timezone
from ephios.core.models import UserProfile
from ephios.core.models.users import Notification
from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate
from ephios_shift_coordination.services import create_period
from ephios_shift_coordination.surveys import open_survey, process_surveys, save_response
coordinator = UserProfile.objects.get(email='demo-001@example.invalid')
period = create_period(coordinator, template_id=ServiceTemplate.objects.get(title='Dienst').pk,
    start_date=date(2032, 4, 1), end_date=date(2032, 5, 31), dates=[date(2032, 4, 2)],
    rules=PlanningSettings.objects.get(pk=1).snapshot(), creation_key=uuid.uuid4())
period = open_survey(coordinator, period.pk, expected_version=1)
ids = {}
for index in (3, 5):
    person = UserProfile.objects.get(email=f'demo-{index:03d}@example.invalid')
    response = period.responses.get(user=person)
    save_response(person, period.pk, expected_version=0, maximum=0 if index == 3 else 2,
        notes='<script>window.compromised=true</script>',
        ratings={pk: 'unavailable' if index == 3 else 'preferred'
                 for pk in response.offered_shifts.values_list('pk', flat=True)})
    ids[str(index)] = person.pk
period.deadline = timezone.now() - timedelta(seconds=1)
period.save(update_fields=['deadline'])
process_surveys()
print(json.dumps({'period': period.pk, 'shift': period.events.first().shifts.first().pk,
                  'people': ids, 'notifications': Notification.objects.count()}))
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
                expect(
                    page.get_by_role("heading", name="Dienste planen", exact=False)
                ).to_be_visible()
            uid, sid = setup["people"]["5"], setup["shift"]
            selector = f'input[data-person="{uid}"][data-shift="{sid}"]'
            count = first.locator(f'[data-person-count="{uid}"]')
            for page in (first, second):
                for card in page.locator(".sc-candidates > summary").all():
                    card.click()
            first.locator(selector).focus()
            first.keyboard.press("Space")
            expect(count).to_have_text("1/2")
            first.keyboard.press("Space")
            expect(count).to_have_text("0/2")
            first.locator(selector).check()
            second.locator(selector).check()
            expect(
                second.get_by_role("button", name="Gemeinsamen Entwurf speichern")
            ).to_be_enabled()
            # The whole period is on one page and only weekdays with shifts get a column.
            expect(first.locator(".sc-day-box")).to_have_count(1)
            expect(first.locator(selector)).to_be_checked()
            # A note exists, so the person is marked and the notes counter filters the list.
            person = first.locator("#planning-people details").filter(
                has=first.locator(f'[data-person-count="{uid}"]')
            )
            person.locator("summary").click()
            expect(
                person.get_by_text("<script>window.compromised=true</script>", exact=True)
            ).to_be_visible()
            assert first.evaluate("window.compromised") is None
            first.get_by_role("button", name="Hinweise", exact=False).click()
            expect(first.locator("#planning-people .sc-person:not([hidden])")).to_have_count(2)
            first.get_by_role("button", name="Hinweise", exact=False).click()
            card = first.locator(f'[data-shift-card="{sid}"]')
            card.get_by_label(
                "Person hinzufügen, die diese Schicht nicht angeboten hat"
            ).select_option(str(setup["people"]["3"]))
            card.get_by_role("button", name="Person hinzufügen", exact=True).click()
            # One banner for every exception, and the rule violations are marked in place.
            expect(first.locator(".sc-banner-list li")).to_have_count(2)
            expect(first.locator(".sc-choice.sc-flagged")).to_have_count(1)
            expect(first.locator("[data-shift-problem] .sc-problem")).to_have_count(0)
            expect(
                first.get_by_role("button", name="Gemeinsamen Entwurf speichern")
            ).to_be_enabled()
            first.get_by_label("Ich bestätige diese Ausnahmen", exact=False).check()
            first.get_by_label("Gemeinsame Anmerkung für den Nachweis", exact=False).fill(
                "Mit der Person abgesprochen"
            )
            first.get_by_role("button", name="Gemeinsamen Entwurf speichern").click()
            expect(first.locator("#draft-status")).to_have_text("Gemeinsamer Entwurf gespeichert")
            # The second page staffs the same two-person shift alone, which needs a confirmation.
            second.get_by_label("Ich bestätige diese Ausnahmen", exact=False).check()
            with second.expect_response(lambda response: response.url.endswith("/draft/")) as stale:
                second.get_by_role("button", name="Gemeinsamen Entwurf speichern").click()
            assert stale.value.status == 409
            expect(second.locator("#draft-status")).to_contain_text("Neu laden")
            first.reload()
            expect(first.locator(selector)).to_be_checked()
            expect(
                first.locator(f'input[data-person="{setup["people"]["3"]}"][data-shift="{sid}"]')
            ).to_be_checked()
            first.get_by_text("Dokumentierte Ausnahmen", exact=True).click()
            expect(first.get_by_text("Mit der Person abgesprochen", exact=True)).to_have_count(2)
            result = json.loads(
                in_test_app(f"""
import json
from ephios.core.models import LocalParticipation
from ephios.core.models.users import Notification
from ephios_shift_coordination.models import DraftAssignment, RuleOverride
print(json.dumps({{'draft': DraftAssignment.objects.filter(period_id={setup["period"]}).count(),
    'overrides': RuleOverride.objects.filter(period_id={setup["period"]}).count(),
    'native': LocalParticipation.objects.filter(
        shift__plannedshift__event__period_id={setup["period"]}).count(),
    'notifications': Notification.objects.count()}}))
""")
            )
            assert result == {
                "draft": 2,
                "overrides": 2,
                "native": 0,
                "notifications": setup["notifications"],
            }
            first.screenshot(path=".local/test-results/planning-draft.png", full_page=True)
        except Exception:
            capture_failure(first, "draft-first-failure")
            capture_failure(second, "draft-second-failure")
            raise
        finally:
            browser.close()
