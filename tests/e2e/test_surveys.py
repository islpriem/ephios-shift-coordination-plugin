import json
import os
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright

from scripts.project import compose
from tests.e2e.test_planning import capture_failure, login

pytestmark = pytest.mark.e2e


def in_test_app(code):
    assert os.environ["EPHIOS_STACK"] == "test"
    result = compose(
        "exec",
        "-T",
        "app",
        "ephios",
        "shell",
        "-c",
        "import os\nassert os.environ['EPHIOS_TESTING'] == '1'\n" + code,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()[-1]


def mail_messages():
    port = os.environ.get("EPHIOS_MAIL_PORT", "8100")
    with urlopen(f"http://127.0.0.1:{port}/api/v1/messages?limit=1000", timeout=10) as response:
        return json.load(response)["messages"]


def test_german_mobile_survey_invitations_reminders_and_deadline():
    template_id = int(
        in_test_app("""
import uuid
from ephios.core.models import EventType
from ephios_shift_coordination.models import ServiceTemplate, ShiftTemplate
source = ServiceTemplate.objects.get(title='Dienst')
template = ServiceTemplate.objects.create(
    title=f'Acceptance {uuid.uuid4()}', location='Synthetic test',
    event_type=EventType.objects.create(title=f'Acceptance {uuid.uuid4()}'))
template.visible_for.set(source.visible_for.all())
template.responsible_groups.set(source.responsible_groups.all())
for original in source.shifts.all():
    values = {field.name: getattr(original, field.name) for field in ShiftTemplate._meta.fields
        if field.name not in ('id', 'template')}
    shift = ShiftTemplate.objects.create(template=template, **values)
    shift.qualifications.set(original.qualifications.all())
print(template.pk)
""")
    )
    base = f"http://127.0.0.1:{os.environ['EPHIOS_HTTP_PORT']}"
    mail_before = {message["ID"] for message in mail_messages()}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        coordinator = browser.new_page(service_workers="block", locale="de-DE")
        member = browser.new_page(
            service_workers="block", locale="de-DE", viewport={"width": 390, "height": 844}
        )
        second = browser.new_page(service_workers="block", locale="de-DE")
        third = browser.new_page(service_workers="block", locale="de-DE")
        try:
            login(coordinator, "demo-001@example.invalid", "demo")
            coordinator.goto(f"{base}/shift-coordination/planning/new/")
            coordinator.locator('[name="template"]').select_option(str(template_id))
            coordinator.locator('[name="start_date"]').fill("2031-04-01")
            coordinator.locator('[name="end_date"]').fill("2031-04-30")
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Termine vorschlagen").click()
            for checkbox in coordinator.locator('[name="dates"]').all():
                checkbox.uncheck()
            coordinator.locator('[name="dates"][value="2031-04-02"]').check()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Dienste anlegen", exact=False).click()
            period_id = int(coordinator.url.rstrip("/").split("/")[-1])
            local_deadline_time = (
                coordinator.locator('[name="deadline"]').input_value().split("T")[1]
            )
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role(
                    "button", name="Umfrage öffnen und Mitglieder einladen"
                ).click()
            expect(
                coordinator.get_by_role("heading", name="Rückmeldungen", exact=True)
            ).to_be_visible()
            in_test_app(
                "from django.core.management import call_command\n"
                'call_command("run_periodic")\nprint("sent")'
            )
            invitations = [
                message for message in mail_messages() if message["ID"] not in mail_before
            ]
            member_mail = next(
                message
                for message in invitations
                if any(
                    recipient["Address"] == "demo-003@example.invalid"
                    for recipient in message["To"]
                )
            )
            assert (
                "Dienstplanung" in member_mail["Subject"]
                and "Wann kannst du?" in member_mail["Subject"]
            )
            port = os.environ["EPHIOS_MAIL_PORT"]
            with urlopen(
                f"http://127.0.0.1:{port}/api/v1/message/{member_mail['ID']}", timeout=10
            ) as response:
                body = json.load(response)
            assert f"/shift-coordination/surveys/{period_id}/" in body["Text"]
            assert "noch keine Einteilung" in " ".join(body["Text"].split())
            assert "persönlichen Höchstzahl" in " ".join(body["Text"].split())
            assert local_deadline_time in body["Text"]
            login(member, "demo-003@example.invalid", "demo")
            member.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(
                member.get_by_role("heading", name="Deine Verfügbarkeit", exact=True)
            ).to_be_visible()
            expect(member.get_by_label("Besonders gern", exact=False)).to_have_count(1)
            member.get_by_label("persönliche Höchstzahl", exact=False).fill("0")
            member.get_by_label("Anmerkungen für die Koordination", exact=False).fill(
                "<script>Nur für Koordination</script>"
            )
            member.locator('input[value="preferred"]').first.check()
            member.get_by_role("button", name="Antwort speichern").focus()
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.keyboard.press("Enter")
            expect(member.get_by_label("persönliche Höchstzahl", exact=False)).to_have_value("0")
            expect(member.get_by_text("Antwort gespeichert am", exact=False)).to_be_visible()
            member.get_by_label("persönliche Höchstzahl", exact=False).fill("1")
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.get_by_role("button", name="Antwort speichern").click()
            expect(member.get_by_text("Danke, deine Antwort wurde gespeichert.")).to_be_visible()
            assert member.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            login(second, "demo-023@example.invalid", "demo")
            second.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(
                second.get_by_label("Anmerkungen für die Koordination", exact=False)
            ).to_have_value("")
            second.get_by_label("persönliche Höchstzahl", exact=False).fill("1")
            second.locator('input[value="if_needed"]').first.check()
            with second.expect_navigation(wait_until="domcontentloaded"):
                second.get_by_role("button", name="Antwort speichern").click()
            login(third, "demo-004@example.invalid", "demo")
            third.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(
                third.get_by_label("Anmerkungen für die Koordination", exact=False)
            ).to_have_value("")
            coordinator.reload()
            detail = coordinator.locator("details").filter(has_text="Nur für Koordination")
            detail.locator("summary").click()
            expect(
                detail.get_by_text("<script>Nur für Koordination</script>", exact=True)
            ).to_be_visible()
            count = in_test_app(f"""
from datetime import timedelta
from django.utils import timezone
from django.core.management import call_command
from unittest.mock import patch
from ephios_shift_coordination.models import PlanningPeriod, NotificationDispatch
period = PlanningPeriod.objects.get(pk={period_id})
with patch('django.utils.timezone.now', return_value=period.deadline - timedelta(days=2)):
    call_command('run_periodic')
    call_command('run_periodic')
records = NotificationDispatch.objects.filter(period=period, kind='reminder', skipped=False)
answered = ['demo-003@example.invalid', 'demo-023@example.invalid']
assert not records.filter(user__email__in=answered).exists()
assert records.filter(user__email='demo-004@example.invalid').count() == 1
print(records.count(), period.responses.exclude(user__email__in=answered).count())
""")
            sent, invited = (int(value) for value in count.split())
            # Everybody invited but the two who answered is reminded exactly once.
            assert sent == invited > 10
            reminders = [
                message
                for message in mail_messages()
                if message["ID"] not in mail_before and message["Subject"].startswith("Erinnerung:")
            ]
            current_reminders = []
            for message in reminders:
                if not any(
                    recipient["Address"] in ("demo-003@example.invalid", "demo-004@example.invalid")
                    for recipient in message["To"]
                ):
                    continue
                with urlopen(
                    f"http://127.0.0.1:{port}/api/v1/message/{message['ID']}", timeout=10
                ) as result:
                    if f"/surveys/{period_id}/" in json.load(result)["Text"]:
                        current_reminders.append(message)
            assert len(current_reminders) == 1
            assert current_reminders[0]["To"][0]["Address"] == "demo-004@example.invalid"
            in_test_app(f"""
from datetime import timedelta
from django.utils import timezone
from ephios_shift_coordination.models import PlanningPeriod
PlanningPeriod.objects.filter(pk={period_id}).update(deadline=timezone.now() - timedelta(seconds=1))
print('deadline reached')
""")
            with third.expect_navigation(wait_until="domcontentloaded"):
                third.get_by_label("persönliche Höchstzahl", exact=False).fill("1")
                third.locator('input[value="available"]').first.check()
                third.get_by_role("button", name="Antwort speichern").click()
            expect(third.get_by_text("Die Umfrage ist geschlossen.", exact=False)).to_be_visible()
            expect(third.get_by_role("button", name="Antwort speichern")).to_have_count(0)
            coordinator.reload()
            expect(coordinator.get_by_text("Planung", exact=False).first).to_be_visible()
            in_test_app(f"""
from django.core.management import call_command
from ephios_shift_coordination.models import PlanningPeriod
call_command('run_periodic')
assert PlanningPeriod.objects.get(pk={period_id}).state == 'planning'
print('closed')
""")
            member.reload()
            expect(member.get_by_label("persönliche Höchstzahl", exact=False)).to_have_value("1")
            expect(member.get_by_label("persönliche Höchstzahl", exact=False)).to_be_disabled()
            member.screenshot(path=".local/test-results/survey-mobile-de.png", full_page=True)
            from tests.e2e.publication_flow import complete_publication

            complete_publication(
                browser,
                coordinator,
                member,
                third,
                base,
                period_id,
                in_test_app,
                mail_messages,
                port,
            )
        except Exception:
            capture_failure(coordinator, "survey-coordinator-failure")
            capture_failure(member, "survey-member-failure")
            capture_failure(third, "survey-deadline-failure")
            raise
        finally:
            browser.close()
