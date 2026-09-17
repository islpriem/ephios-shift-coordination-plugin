import json
import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_planning import capture_failure, login
from tests.e2e.test_surveys import in_test_app, mail_messages

pytestmark = pytest.mark.e2e


def test_sitting_in_early_closing_and_self_service_replacement():
    setup = json.loads(
        in_test_app("""
import json, uuid
from datetime import date
from ephios.core.models import EventType, UserProfile
from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate, ShiftTemplate
from ephios_shift_coordination.services import create_period
from ephios_shift_coordination.surveys import open_survey
# An own event type keeps repeated runs of this test independent of each other.
source = ServiceTemplate.objects.get(title='Dienst')
template = ServiceTemplate.objects.create(title=f'Beisitz {uuid.uuid4()}',
    location='Synthetic test', event_type=EventType.objects.create(title=f'Beisitz {uuid.uuid4()}'))
template.visible_for.set(source.visible_for.all())
template.responsible_groups.set(source.responsible_groups.all())
for original in source.shifts.all():
    values = {field.name: getattr(original, field.name) for field in ShiftTemplate._meta.fields
        if field.name not in ('id', 'template')}
    shift = ShiftTemplate.objects.create(template=template, **values)
    shift.qualifications.set(original.qualifications.all())
rules = PlanningSettings.objects.get(pk=1).snapshot()
rules['allow_observers'] = True
coordinator = UserProfile.objects.get(email='demo-001@example.invalid')
period = create_period(coordinator, template_id=template.pk,
    start_date=date(2036, 4, 1), end_date=date(2036, 4, 30), dates=[date(2036, 4, 2)],
    rules=rules, creation_key=uuid.uuid4())
period = open_survey(coordinator, period.pk, expected_version=1)
shifts = list(period.events.first().shifts.order_by('pk'))
print(json.dumps({'period': period.pk, 'service': period.events.first().pk,
    'shifts': [s.pk for s in shifts],
    'people': {str(i): UserProfile.objects.get(email=f'demo-{i:03d}@example.invalid').pk
               for i in (3, 4)}}))
""")
    )
    base = f"http://127.0.0.1:{os.environ['EPHIOS_HTTP_PORT']}"
    first, second = setup["shifts"]
    sitter, regular = setup["people"]["3"], setup["people"]["4"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        coordinator = browser.new_page(
            service_workers="block", locale="de-DE", viewport={"width": 1600, "height": 1000}
        )
        member = browser.new_page(service_workers="block", locale="de-DE")
        helper = browser.new_page(service_workers="block", locale="de-DE")
        try:
            # A member offers one shift and, in addition, to sit in on the other one.
            login(member, "demo-003@example.invalid", "demo")
            member.goto(f"{base}/shift-coordination/surveys/{setup['period']}/")
            member.get_by_label("persönliche Höchstzahl", exact=False).fill("2")
            expect(member.get_by_label("Besonders gern", exact=False)).to_have_count(1)
            member.locator('input[value="preferred"]').first.check()
            member.get_by_label("beisitzen", exact=False).check()
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.get_by_role("button", name="Antwort speichern").click()
            expect(member.get_by_text("Danke, deine Antwort wurde gespeichert.")).to_be_visible()
            login(helper, "demo-004@example.invalid", "demo")
            helper.goto(f"{base}/shift-coordination/surveys/{setup['period']}/")
            helper.get_by_label("persönliche Höchstzahl", exact=False).fill("1")
            helper.locator('input[value="available"]').first.check()
            with helper.expect_navigation(wait_until="domcontentloaded"):
                helper.get_by_role("button", name="Antwort speichern").click()
            # The coordination closes the survey early instead of waiting for the deadline.
            login(coordinator, "demo-001@example.invalid", "demo")
            coordinator.goto(f"{base}/shift-coordination/planning/{setup['period']}/")
            coordinator.get_by_text("Umfrage vorzeitig schließen").click()
            coordinator.get_by_label("Umfrage jetzt schließen", exact=False).check()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Umfrage jetzt schließen").click()
            expect(coordinator.get_by_role("heading", name="Dienste planen")).to_be_visible()
            for shift, people in ((first, [sitter]), (second, [regular, sitter])):
                card = coordinator.locator(f'[data-shift-card="{shift}"]')
                card.locator(".sc-candidates > summary").click()
                for person in people:
                    card.locator(f'input[data-person="{person}"]').first.check()
            expect(coordinator.locator("input[data-person]:checked")).to_have_count(3)
            # One shift stays below its minimum on purpose: that is a confirmed exception now.
            expect(coordinator.locator(".sc-banner-list li")).to_have_count(1)
            expect(coordinator.locator("[data-shift-problem] .sc-problem")).to_have_count(1)
            coordinator.get_by_label("Ich bestätige diese Ausnahmen", exact=False).check()
            coordinator.get_by_role("button", name="Gemeinsamen Entwurf speichern").click()
            expect(coordinator.locator("#draft-status")).to_have_text(
                "Gemeinsamer Entwurf gespeichert"
            )
            before_mail = {message["ID"] for message in mail_messages()}
            coordinator.get_by_role("link", name="Prüfen und veröffentlichen").click()
            for token in coordinator.locator('[name="confirmed_tokens"]').all():
                token.check()
            coordinator.get_by_label(
                "Ich veröffentliche, obwohl in einigen Schichten Personen fehlen."
            ).check()
            coordinator.get_by_label("Ich habe den Dienstplan geprüft", exact=False).check()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Dienstplan veröffentlichen").click()
            expect(
                coordinator.get_by_role("heading", name="Veröffentlichter Dienstplan")
            ).to_be_visible()
            expect(coordinator.get_by_text("Beisitz", exact=False).first).to_be_visible()
            assert (
                in_test_app(f"""
from ephios.core.models import LocalParticipation
from ephios.core.models.events import PlaceholderParticipation
from ephios_shift_coordination.models import ObserverParticipation
period = {setup["period"]}
print(','.join(str(count) for count in (
    LocalParticipation.objects.filter(shift__plannedshift__event__period_id=period).count(),
    PlaceholderParticipation.objects.filter(shift__plannedshift__event__period_id=period).count(),
    ObserverParticipation.objects.filter(planned_shift__event__period_id=period).count())))
""")
                == "2,1,1"
            )
            # Somebody who is unavailable signs off and the coordination is warned about the gap.
            member.goto(f"{base}/shift-coordination/services/{setup['service']}/")
            expect(
                member.get_by_role("heading", name="Besetzung und Nachbesetzung")
            ).to_be_visible()
            expect(member.get_by_text("Demoperson 004", exact=False).first).to_be_visible()
            # Signing off from the shift that was staffed leaves a gap the coordination must know.
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.locator(f'section[data-shift="{second}"]').get_by_role(
                    "button", name="Ich bin verhindert"
                ).click()
            expect(member.get_by_text("Danke für die Rückmeldung", exact=False)).to_be_visible()
            warnings = [
                message
                for message in mail_messages()
                if message["ID"] not in before_mail
                and message["Subject"].startswith("Schicht nicht mehr ausreichend besetzt")
            ]
            assert warnings and {
                recipient["Address"] for message in warnings for recipient in message["To"]
            } <= {"demo-001@example.invalid", "demo-002@example.invalid"}
            # A qualified member can take the free place without the coordination.
            helper.goto(f"{base}/shift-coordination/services/{setup['service']}/")
            expect(helper.get_by_role("button", name="Ich übernehme die Schicht")).to_have_count(0)
            coordinator.goto(f"{base}/shift-coordination/services/{setup['service']}/")
            expect(coordinator.get_by_text("Personen fehlen", exact=False).first).to_be_visible()
            coordinator.screenshot(path=".local/test-results/replacement-de.png", full_page=True)
        except Exception:
            capture_failure(coordinator, "staffing-coordinator-failure")
            capture_failure(member, "staffing-member-failure")
            raise
        finally:
            browser.close()
