import os

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("language, label", [("en", "Shift coordination"), ("de", "Dienstplanung")])
def test_installed_plugin_appears_in_ephios_settings(language, label):
    base = f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8097')}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(locale=language)
        try:
            page.goto(base + "/accounts/login/")
            page.locator('[name="username"]').fill(f"admin-{language}@example.invalid")
            page.locator('[name="password"]').fill("isolated-browser-test-password")
            page.locator('button[type="submit"]').click()
            page.wait_for_url(base + "/")
            page.goto(base + "/settings/instance/")
            expect(page.locator("form").get_by_text(label, exact=True)).to_be_visible()
        except Exception:
            page.screenshot(path=".local/test-results/e2e-failure.png", full_page=True)
            raise
        finally:
            browser.close()
