import os
from contextlib import suppress

import pytest
from playwright.sync_api import Error, expect, sync_playwright

pytestmark = pytest.mark.e2e


def capture_failure(page, filename):
    with suppress(Error):
        page.screenshot(path=f".local/test-results/{filename}.png", timeout=5000)


def login(page, email, password):
    page.goto(f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}/accounts/login/")
    page.locator('[name="username"]').fill(email)
    page.locator('[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    page.wait_for_url(f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}/")


def test_demo_coordinator_creates_service_series_and_member_sees_event():
    base = f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        coordinator = browser.new_page(service_workers="block", locale="de-DE")
        member = None
        try:
            login(coordinator, "demo-001@example.invalid", "demo")
            # Coordinators reach the periods through the menu; members get the surveys directly.
            coordinator.get_by_role("button", name="Dienstplanung", exact=True).click()
            coordinator.get_by_role("link", name="Planungsintervalle", exact=True).click()
            coordinator.get_by_role("link", name="Neues Planungsintervall").click()
            coordinator.locator('[name="template"]').select_option(label="Dienst")
            coordinator.locator('[name="start_date"]').fill("2030-04-01")
            coordinator.locator('[name="end_date"]').fill("2030-04-07")
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Termine vorschlagen").click()
            for checkbox in coordinator.locator('[name="dates"]').all():
                checkbox.uncheck()
            coordinator.locator('[name="dates"][value="2030-04-02"]').check()
            expect(coordinator.get_by_text("1 / 7", exact=True)).to_be_visible()
            expect(coordinator.get_by_text("Prüfen und anlegen", exact=False)).to_be_visible()
            with coordinator.expect_navigation(wait_until="domcontentloaded"):
                coordinator.get_by_role("button", name="Dienste anlegen", exact=False).click()
            expect(coordinator.get_by_role("heading", name="Dienst", exact=True)).to_be_visible()
            event = coordinator.locator('main a[href*="/events/"]').first
            event_path = event.get_attribute("href")
            event.click()
            expect(coordinator.get_by_text("Schicht 1", exact=True)).to_be_visible()
            expect(coordinator.get_by_text("Schicht 2", exact=True)).to_be_visible()
            member = browser.new_page(
                service_workers="block", locale="de-DE", viewport={"width": 390, "height": 844}
            )
            login(member, "demo-003@example.invalid", "demo")
            member.locator(".navbar-toggler").click()
            expect(member.get_by_role("button", name="Dienstplanung", exact=True)).to_have_count(0)
            expect(member.get_by_role("link", name="Umfragen", exact=True)).to_be_visible()
            member.goto(base + event_path)
            expect(member.get_by_text("Schicht 1", exact=True)).to_be_visible()
            expect(member.get_by_text("Schicht 2", exact=True)).to_be_visible()
            expect(member.get_by_text("Dienstplanung", exact=False).first).to_be_visible()
            assert member.locator('form[action*="signup"] button').count() == 0
        except Exception:
            capture_failure(coordinator, "planning-coordinator-failure")
            if member:
                capture_failure(member, "planning-member-failure")
            raise
        finally:
            browser.close()


def test_admin_can_edit_demo_template_and_add_a_shift():
    base = f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(service_workers="block", locale="de-DE")
        try:
            login(page, "admin-de@example.invalid", "isolated-browser-test-password")
            page.goto(base + "/shift-coordination/templates/")
            page.locator(".list-group a").filter(has_text="Dienst").click()
            total = int(page.locator('[name="shifts-TOTAL_FORMS"]').input_value())
            page.get_by_role("button", name="Schicht hinzufügen").click()
            expect(page.locator('[name="shifts-TOTAL_FORMS"]')).to_have_value(str(total + 1))
            for field, value in {
                "label": "Browser-Schicht",
                "meeting_time": "17:00",
                "start_time": "17:15",
                "end_time": "19:00",
                "minimum": "1",
                "maximum": "1",
            }.items():
                page.locator(f'[name="shifts-{total}-{field}"]').fill(value)
            page.locator('[name="location"]').fill("Browser-Testort")
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("button", name="Vorlage speichern").click()
            expect(page.get_by_role("heading", name="Dienstvorlagen", exact=True)).to_be_visible()
            page.locator(".list-group a").filter(has_text="Dienst").click()
            expect(page.locator('[name="location"]')).to_have_value("Browser-Testort")
            added = page.locator("fieldset").filter(
                has=page.locator('input[value="Browser-Schicht"]')
            )
            expect(added).to_have_count(1)
            added.locator('[name$="-DELETE"]').check()
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("button", name="Vorlage speichern").click()
        except Exception:
            capture_failure(page, "template-failure")
            raise
        finally:
            browser.close()
