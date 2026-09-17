"""Continue the real survey workflow through publication and native calendar integration."""

import json
from html.parser import HTMLParser
from urllib.request import urlopen

from icalendar import Calendar
from playwright.sync_api import Error, expect


def personal_feed(page, base):
    class CalendarURL(HTMLParser):
        value = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("id") == "calendar-url":
                self.value = attrs["value"]

    # Read the existing settings through this person's session; never log or save its token.
    settings = page.request.get(base + "/settings/calendar/")
    parser = CalendarURL()
    parser.feed(settings.text())
    assert parser.value is not None, "Native calendar settings did not provide a URL."
    try:
        response = page.request.get(parser.value)
    except Error:
        raise AssertionError("The native personal calendar request failed.") from None
    status = response.status
    assert status == 200
    return {
        str(event["UID"]).split("@")[0]: event
        for event in Calendar.from_ical(response.body()).walk("VEVENT")
    }


def complete_publication(
    browser, coordinator, member, third, base, period_id, app, mail_messages, mail_port
):
    info = json.loads(
        app(f"""
import json
from ephios.core.models import UserProfile
from ephios_shift_coordination.models import PlanningPeriod
period = PlanningPeriod.objects.get(pk={period_id})
shifts = list(period.events.first().shifts.order_by('pk'))
print(json.dumps({{'planned': [s.pk for s in shifts], 'native': [s.shift_id for s in shifts],
    'users': {{str(i): UserProfile.objects.get(email=f'demo-{{i:03d}}@example.invalid').pk
        for i in (3, 4, 7)}}}}))
""")
    )
    coordinator.goto(f"{base}/shift-coordination/planning/{period_id}/plan/")
    with coordinator.expect_response(
        lambda r: r.url.endswith("/propose/"), timeout=45000
    ) as calculation:
        coordinator.get_by_role("button", name="Vorschlag berechnen", exact=True).click()
    result = calculation.value.json()
    assert result["status"] == "optimal_primary" and result["score"]["filled"] == 1
    coordinator.get_by_role("button", name="Diesen Vorschlag übernehmen", exact=True).click()
    expect(coordinator.locator("input[data-person]:checked")).to_have_count(2)
    card = coordinator.locator(f'[data-shift-card="{info["planned"][1]}"]')
    card.locator(".sc-candidates > summary").click()
    card.get_by_label("Person hinzufügen, die diese Schicht nicht angeboten hat").select_option(
        str(info["users"]["4"])
    )
    card.get_by_role("button", name="Person hinzufügen", exact=True).click()
    banner = coordinator.locator(".sc-banner")
    # The person is one exception; the half staffed two-person shift is the second.
    expect(banner.locator("li")).to_have_count(2)
    banner.get_by_label("Ich bestätige diese Ausnahmen", exact=False).check()
    banner.get_by_label("Gemeinsame Anmerkung für den Nachweis", exact=False).fill(
        "Telefonisch abgestimmt"
    )
    coordinator.get_by_role("button", name="Gemeinsamen Entwurf speichern", exact=True).click()
    expect(coordinator.locator("#draft-status")).to_have_text("Gemeinsamer Entwurf gespeichert")
    coordinator.get_by_role("link", name="Prüfen und veröffentlichen").click()
    expect(coordinator.get_by_text("Telefonisch abgestimmt", exact=True).first).to_be_visible()
    other = browser.new_page(service_workers="block", locale="de-DE")
    from tests.e2e.test_planning import login

    login(other, "demo-002@example.invalid", "demo")
    review_url = f"{base}/shift-coordination/planning/{period_id}/publication/"
    other.goto(review_url)
    assert str(info["native"][0]) not in personal_feed(member, base)
    assert (
        app(
            "from ephios.core.models import LocalParticipation\n"
            f"print(LocalParticipation.objects.filter(shift__plannedshift__event__period_id={period_id})"
            ".count())"
        )
        == "0"
    )
    before_mail = {m["ID"] for m in mail_messages()}
    for page in (coordinator, other):
        for token in page.locator('[name="confirmed_tokens"]').all():
            token.check()
        page.get_by_label(
            "Ich veröffentliche, obwohl in einigen Schichten Personen fehlen."
        ).check()
        page.get_by_label("Ich habe den Dienstplan geprüft", exact=False).check()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("button", name="Dienstplan veröffentlichen", exact=True).click()
        expect(
            page.get_by_role("heading", name="Veröffentlichter Dienstplan", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Dienstplan veröffentlichen")).to_have_count(0)
    saved_digest = app(f"""
import hashlib, json
from ephios.core.models import LocalParticipation
from ephios_shift_coordination.models import PlanningPeriod, NotificationDispatch
period = PlanningPeriod.objects.get(pk={period_id})
assert period.state == 'published'
assert LocalParticipation.objects.filter(
    shift__plannedshift__event__period=period, state=1).count() == 3
assert NotificationDispatch.objects.filter(period=period, kind='publication').count() == 3
print(hashlib.sha256(json.dumps(period.publication_snapshot, sort_keys=True).encode()).hexdigest())
""")
    summaries = [
        m
        for m in mail_messages()
        if m["ID"] not in before_mail
        and m["Subject"].startswith("Dienstplan")
        and m["Subject"].endswith("veröffentlicht")
    ]
    assert len(summaries) == 3
    assert {m["To"][0]["Address"] for m in summaries} == {
        "demo-003@example.invalid",
        "demo-004@example.invalid",
        "demo-023@example.invalid",
    }
    for message in summaries:
        with urlopen(
            f"http://127.0.0.1:{mail_port}/api/v1/message/{message['ID']}", timeout=10
        ) as response:
            body = json.load(response)["Text"]
        assert "veröffentlicht" in body and "/events/" in body
        assert "Hallo Demoperson" in body
        assert "Nur für Koordination" not in body and "Telefonisch abgestimmt" not in body
    member.reload()
    expect(member.get_by_text("Deine Schichten im veröffentlichten Dienstplan")).to_be_visible()
    expect(member.get_by_role("link", name="Ersatz finden").first).to_be_visible()
    first_feed = personal_feed(member, base)
    assert str(info["native"][0]) in first_feed
    assert str(first_feed[str(info["native"][0])]["STATUS"]) == "CONFIRMED"
    assert str(info["native"][1]) in personal_feed(third, base)
    coordinator.screenshot(path=".local/test-results/published-plan-de.png", full_page=True)
    # Use the normal native disposition UI to replace one person after publication.
    coordinator.goto(f"{base}/shifts/{info['native'][0]}/disposition/")
    old = coordinator.locator(f'[data-participant-id="{info["users"]["3"]}"]')
    old.drag_to(coordinator.locator('[data-drop-to-state="3"]'))
    expect(old.locator('[name$="-state"]')).to_have_value("3")
    coordinator.locator(".select2-selection").click()
    coordinator.get_by_role("searchbox").fill("Demoperson 007")
    coordinator.get_by_role("option", name="Demoperson 007", exact=False).click()
    new = coordinator.locator(f'[data-participant-id="{info["users"]["7"]}"]')
    expect(new).to_be_visible()
    new.drag_to(coordinator.locator('[data-drop-to-state="1"]'))
    expect(new.locator('[name$="-state"]')).to_have_value("1")
    with coordinator.expect_navigation(wait_until="domcontentloaded"):
        coordinator.get_by_role("button", name="Speichern", exact=True).click()
    coordinator.goto(review_url)
    expect(
        coordinator.get_by_text(
            "Die aktuellen ephios-Daten weichen von diesem Veröffentlichungsnachweis ab.",
            exact=True,
        )
    ).to_be_visible()
    expect(coordinator.get_by_text("In ephios geändert", exact=False)).to_be_visible()
    assert (
        app(f"""
import hashlib, json
from ephios_shift_coordination.models import PlanningPeriod
period = PlanningPeriod.objects.get(pk={period_id})
print(hashlib.sha256(json.dumps(period.publication_snapshot, sort_keys=True).encode()).hexdigest())
""")
        == saved_digest
    )
    assert str(info["native"][0]) not in personal_feed(member, base)
    replacement = browser.new_page(service_workers="block", locale="de-DE")
    login(replacement, "demo-007@example.invalid", "demo")
    assert str(info["native"][0]) in personal_feed(replacement, base)
    # After the native rebooking the member has no shift in this period any more.
    member.reload()
    expect(member.get_by_text("Deine Schichten im veröffentlichten Dienstplan")).to_have_count(0)
    expect(member.get_by_text("Die Umfrage ist geschlossen.", exact=False)).to_be_visible()
    coordinator.screenshot(path=".local/test-results/published-plan-drift-de.png", full_page=True)
