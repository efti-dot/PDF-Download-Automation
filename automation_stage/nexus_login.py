import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, BrowserContext
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

load_dotenv()

CONFIG = {
    "login_url": "https://network.infornexus.com/login",
    "username_selector": "input[name='userid']",
    "password_selector": "input[name='uPassword']",
    "submit_selector": "#loginButton",
    "logged_in_selector": "input[name='ordersFilterOrder']",
    "error_selector": "",
    "timeout_ms": 30_000,
}

STATE_FILE = Path(".auth/nexus_state.json")
SCREENSHOT_DIR = Path("output/screenshots")


class LoginError(Exception):
    pass


def _screenshot(page: Page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[info] screenshot saved: {path}")
    except Exception:
        pass


screenshot = _screenshot


def is_logged_in(page: Page, timeout_ms: int = 8_000) -> bool:
    """True if the 'logged in' element is visible on the current page."""
    try:
        page.wait_for_selector(
            CONFIG["logged_in_selector"], state="visible", timeout=timeout_ms
        )
        return True
    except PWTimeout:
        return False


def login(page: Page, username: str, password: str) -> None:
    """Perform the login once. Raises LoginError on failure."""
    page.goto(CONFIG["login_url"], wait_until="domcontentloaded")

    page.wait_for_selector(CONFIG["username_selector"], timeout=CONFIG["timeout_ms"])
    page.fill(CONFIG["username_selector"], username)
    page.fill(CONFIG["password_selector"], password)
    page.click(CONFIG["submit_selector"])

    # Wait for success. If an error element is configured, also detect failure.
    try:
        page.wait_for_selector(
            CONFIG["logged_in_selector"], state="visible", timeout=CONFIG["timeout_ms"]
        )
    except PWTimeout:
        _screenshot(page, "login_failed")
        if CONFIG["error_selector"] and page.locator(CONFIG["error_selector"]).count():
            msg = page.locator(CONFIG["error_selector"]).first.inner_text()
            raise LoginError(f"Login rejected by Nexus: {msg.strip()}")
        raise LoginError(
            "Login did not complete (wrong credentials, slow page, or the "
            "'logged_in_selector' is wrong). Check output/screenshots/login_failed.png"
        )


def get_logged_in_context(p, headless: bool = False):
    """
    Returns (browser, context, page) that are logged in.
    Reuses the saved session when it is still valid, otherwise logs in.
    """
    username = os.getenv("NEXUS_USERNAME")
    password = os.getenv("NEXUS_PASSWORD")

    browser = p.chromium.launch(headless=headless)
    context: BrowserContext

    # Try the saved session first
    if STATE_FILE.exists():
        context = browser.new_context(
            storage_state=str(STATE_FILE), accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(CONFIG["timeout_ms"])
        page.goto(CONFIG["login_url"], wait_until="domcontentloaded")
        if is_logged_in(page):
            print("[ok] Reused saved session (no login needed).")
            return browser, context, page
        print("[info] Saved session expired. Logging in again.")
        context.close()

    # Fresh login
    if not username or not password:
        browser.close()
        raise LoginError("NEXUS_USERNAME / NEXUS_PASSWORD missing in .env")

    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(CONFIG["timeout_ms"])
    try:
        login(page, username, password)
    except Exception:
        browser.close()
        raise

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(STATE_FILE))
    print("[ok] Logged in and session saved.")
    return browser, context, page


if __name__ == "__main__":
    headless = "--headless" in sys.argv
    with sync_playwright() as p:
        try:
            browser, context, page = get_logged_in_context(p, headless=headless)
        except LoginError as e:
            print(f"[error] {e}")
            sys.exit(1)

        print(f"[ok] Current page: {page.url}")
        if not headless:
            input("Press Enter to close the browser...")
        browser.close() #python app/nexus_login.py