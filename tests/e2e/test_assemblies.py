"""Calling an assembly quietly, inviting afterwards and answering from the mail."""

import json
import os
import re
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_planning import capture_failure, login
from tests.e2e.test_surveys import in_test_app, mail_messages

pytestmark = pytest.mark.e2e


def mail_body(identifier):
    port = os.environ.get("EPHIOS_MAIL_PORT", "8100")
    with urlopen(f"http://127.0.0.1:{port}/api/v1/message/{identifier}", timeout=10) as result:
        return json.load(result)["Text"]


def test_an_assembly_is_called_quietly_invited_later_and_answered_from_the_mail():
    in_test_app("""
import uuid
from ephios.core.models import EventType
from ephios_shift_coordination.models import ServiceTemplate
source = ServiceTemplate.objects.get(title='Dienst')
event_type = EventType.objects.create(title='Mitgliederversammlung')
event_type.preferences['shift_coordination__is_assembly'] = True
event_type.preferences['shift_coordination__assembly_title'] = 'Mitgliederversammlung'
event_type.preferences['shift_coordination__assembly_location'] = 'Wache'
event_type.preferences['visible_for'] = list(source.visible_for.all())
event_type.preferences['responsible_groups'] = list(source.responsible_groups.all())
print(event_type.pk)
""")
    base = f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}"
    before = {message["ID"] for message in mail_messages()}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        coordinator = browser.new_page(service_workers="block", locale="de-DE")
        invitee = browser.new_page(service_workers="block", locale="de-DE")
        try:
            login(coordinator, "demo-001@example.invalid", "demo")
            coordinator.goto(f"{base}/shift-coordination/assemblies/new/")
            # The recipients are named before anything is sent.
            expect(coordinator.get_by_role("heading", name="Wer eingeladen wird")).to_be_visible()
            expect(coordinator.get_by_text("Demoperson 003", exact=False).first).to_be_visible()
            coordinator.locator('[name="date"]').fill("2031-06-12")
            coordinator.locator('[name="start"]').fill("19:00")
            coordinator.locator('[name="end"]').fill("21:00")
            coordinator.locator('[name="agenda"]').fill("Bericht des Vorstands\nWahlen")
            coordinator.locator('[name="silent"]').check()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Versammlung einberufen").click()
            expect(coordinator.get_by_text("Bericht des Vorstands")).to_be_visible()
            expect(coordinator.get_by_text("Bisher wurde niemand eingeladen")).to_be_visible()
            assert not [message for message in mail_messages() if message["ID"] not in before], (
                "a quietly called assembly must not send anything"
            )

            coordinator.locator("summary", has_text="Personen einladen").click()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Einladung jetzt verschicken").click()
            expect(coordinator.get_by_text("Einladung verschickt am", exact=False)).to_be_visible()

            invitation = [
                message
                for message in mail_messages()
                if message["ID"] not in before
                and any(
                    recipient["Address"] == "demo-003@example.invalid"
                    for recipient in message["To"]
                )
            ]
            assert len(invitation) == 1
            body = mail_body(invitation[0]["ID"])
            assert "Bericht des Vorstands" in body and "Wahlen" in body
            assert "Wache" in body and "Du hast noch nicht geantwortet" in body
            link = re.search(r"https?://[^\s)]+/assemblies/answer/[^\s)]+", body)
            assert link, body

            # The signed link answers without any login at all.
            invitee.goto(link.group(0))
            expect(invitee.get_by_text("Du hast noch nicht geantwortet")).to_be_visible()
            with invitee.expect_navigation(wait_until="domcontentloaded"):
                invitee.get_by_role("button", name="Ja, ich bin dabei").click()
            expect(invitee.get_by_text("Danke, deine Antwort wurde gespeichert")).to_be_visible()
            expect(invitee.get_by_text("Du hast zugesagt.")).to_be_visible()

            # The answer is an ordinary ephios participation, not a plugin-only record.
            assert (
                in_test_app("""
from ephios.core.models import LocalParticipation, UserProfile
person = UserProfile.objects.get(email='demo-003@example.invalid')
print(LocalParticipation.objects.filter(
    user=person, shift__event__type__title='Mitgliederversammlung'
).values_list('state', flat=True).first())
""")
                == "1"
            )
        except Exception:
            capture_failure(coordinator, "e2e-assembly-coordinator")
            capture_failure(invitee, "e2e-assembly-invitee")
            raise
        finally:
            browser.close()
