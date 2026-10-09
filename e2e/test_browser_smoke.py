"""Browser smoke test (Playwright, using an installed Chrome/Edge: no browser download).
Runs only when E2E_BASE_URL is set, against a running stack with the seeded demo data:
  E2E_BASE_URL=http://localhost:5173 python -m pytest e2e -p no:cacheprovider
Optional: E2E_PASSWORD (default: demo password), E2E_BROWSER_CHANNEL (chrome | msedge)."""
import os

import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL to run the browser smoke test")
PASSWORD = os.environ.get("E2E_PASSWORD", "eduos-demo-2026")


def login(page, email):
    page.goto(BASE)
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()


def test_student_asks_expands_citation_and_admin_inspects_trace():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("E2E_BROWSER_CHANNEL", "chrome"))
        page = browser.new_page()
        failures = []
        page.on("pageerror", lambda e: failures.append(str(e)))

        # wrong password shows an error, no crash
        page.goto(BASE)
        page.get_by_label("Email").fill("student1@demo.local")
        page.get_by_label("Password").fill("wrong-password")
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("alert")).to_contain_text("Invalid credentials")

        login(page, "student1@demo.local")
        page.get_by_placeholder("e.g. Why does TCP slow start").fill(
            "Why does TCP slow start double the congestion window every RTT, but then stop doubling?")
        page.get_by_role("button", name="Ask", exact=True).click()
        expect(page.get_by_text("Here is what your course material says")).to_be_visible(timeout=20000)
        expect(page.locator("p.meta").first).to_contain_text("TCP congestion control")     # topic label
        cite = page.get_by_role("button", name="[1]")
        cite.click()
        expect(page.locator("blockquote")).to_be_visible()
        expect(page.get_by_text("Demo mode")).to_be_visible()

        # acknowledgment: recorded as a zero-weight self-report; practice is not available yet, so nothing is "verified"
        page.get_by_role("button", name="I understood").click()
        expect(page.get_by_text("Practice is not available yet")).to_be_visible(timeout=15000)

        page.get_by_role("button", name="Sign out").click()
        login(page, "admin@demo.local")
        page.get_by_role("button", name="Load recent runs").click()
        page.locator("ul button.link").first.click()
        expect(page.get_by_text("R6_low_evidence_explain").first).to_be_visible()
        expect(page.get_by_role("heading", name="Citation validation")).to_be_visible()
        assert failures == [], failures
        browser.close()
