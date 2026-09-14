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
    period_id = int(
        in_test_app("""
import uuid
from datetime import date
from ephios.core.models import UserProfile
from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate
from ephios_shift_coordination.services import create_period
period = create_period(UserProfile.objects.get(email='demo-001@example.invalid'),
    template_id=ServiceTemplate.objects.get(title='Dienst').pk,
    start_date=date(2031, 4, 1), end_date=date(2031, 4, 30), dates=[date(2031, 4, 2)],
    rules=PlanningSettings.objects.get(pk=1).snapshot(), creation_key=uuid.uuid4())
print(period.pk)
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
            login(coordinator, "demo-001@example.invalid", "demo-only-member-password")
            coordinator.goto(f"{base}/shift-coordination/planning/{period_id}/")
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
            assert "Einladung zur Verfügbarkeitsumfrage" in member_mail["Subject"]
            port = os.environ["EPHIOS_MAIL_PORT"]
            with urlopen(
                f"http://127.0.0.1:{port}/api/v1/message/{member_mail['ID']}", timeout=10
            ) as response:
                body = json.load(response)
            assert f"/shift-coordination/surveys/{period_id}/" in body["Text"]
            assert "Wünsche sind keine Zusagen" in " ".join(body["Text"].split())
            assert local_deadline_time in body["Text"]
            login(member, "demo-003@example.invalid", "demo-only-member-password")
            member.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(
                member.get_by_role("heading", name="Deine Verfügbarkeit", exact=True)
            ).to_be_visible()
            expect(member.get_by_text("Besonders erwünscht (Stern)", exact=True)).to_be_visible()
            member.get_by_label("Persönliche Höchstzahl", exact=False).fill("0")
            member.get_by_label("Anmerkungen", exact=False).fill(
                "<script>Nur für Koordination</script>"
            )
            member.get_by_label("Besonders erwünscht (Stern)", exact=True).check()
            member.get_by_role("button", name="Vollständige Antwort speichern").focus()
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.keyboard.press("Enter")
            expect(member.get_by_label("Persönliche Höchstzahl", exact=False)).to_have_value("0")
            expect(member.get_by_text("Antwort gespeichert", exact=False)).to_be_visible()
            member.get_by_label("Persönliche Höchstzahl", exact=False).fill("1")
            with member.expect_navigation(wait_until="domcontentloaded"):
                member.get_by_role("button", name="Vollständige Antwort speichern").click()
            expect(member.get_by_text("Version 2", exact=False)).to_be_visible()
            assert member.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            login(second, "demo-043@example.invalid", "demo-only-member-password")
            second.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(second.get_by_label("Anmerkungen", exact=False)).to_have_value("")
            second.get_by_label("Persönliche Höchstzahl", exact=False).fill("1")
            second.get_by_label("Falls nötig (gelb)", exact=True).check()
            with second.expect_navigation(wait_until="domcontentloaded"):
                second.get_by_role("button", name="Vollständige Antwort speichern").click()
            login(third, "demo-004@example.invalid", "demo-only-member-password")
            third.goto(f"{base}/shift-coordination/surveys/{period_id}/")
            expect(third.get_by_label("Anmerkungen", exact=False)).to_have_value("")
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
answered = ['demo-003@example.invalid', 'demo-043@example.invalid']
assert not records.filter(user__email__in=answered).exists()
assert records.filter(user__email='demo-004@example.invalid').count() == 1
print(records.count())
""")
            assert int(count) == 98
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
                third.get_by_label("Persönliche Höchstzahl", exact=False).fill("1")
                third.get_by_label("Verfügbar (grün)", exact=True).check()
                third.get_by_role("button", name="Vollständige Antwort speichern").click()
            expect(
                third.get_by_text("Deine Antwort ist nur noch lesbar.", exact=True)
            ).to_be_visible()
            expect(
                third.get_by_role("button", name="Vollständige Antwort speichern")
            ).to_have_count(0)
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
            expect(member.get_by_label("Persönliche Höchstzahl", exact=False)).to_have_value("1")
            expect(member.get_by_label("Persönliche Höchstzahl", exact=False)).to_be_disabled()
            member.screenshot(path=".local/test-results/survey-mobile-de.png", full_page=True)
        except Exception:
            capture_failure(coordinator, "survey-coordinator-failure")
            capture_failure(member, "survey-member-failure")
            capture_failure(third, "survey-deadline-failure")
            raise
        finally:
            browser.close()
