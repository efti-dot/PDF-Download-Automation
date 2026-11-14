import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

load_dotenv()   #for just testing

CONFIG = {
    "login_url": "https://network.infornexus.com/login",
    "orders_url": "https://network.infornexus.com/en/trade/OrdersView.jsp?legacy=true&grain=Order",
    "username_selector": "input[name='userid']",
    "password_selector": "input[name='uPassword']",
    "submit_selector": "#loginButton",
    "logged_in_selector": "input[name='ordersFilterOrder']",
    "error_selector": "",
    "timeout_ms": 30_000,
}


class LoginError(Exception):
    pass


class OrdersPageError(LoginError):
    """The login worked, but the Orders page could not be opened for this account."""


def _screenshot(page: Page, name: str) -> None:
    folder = Path(os.getenv("NEXUS_SCREENSHOT_DIR", "output/screenshots"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.png"
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
    """
    1) log in, 2) open the Orders page (same for every account).
    Raises LoginError (bad ID/password) or OrdersPageError (Orders page not available).
    """
    timeout = CONFIG["timeout_ms"]
    page.goto(CONFIG["login_url"], wait_until="domcontentloaded")

    page.wait_for_selector(CONFIG["username_selector"], timeout=timeout)
    page.fill(CONFIG["username_selector"], username)
    page.fill(CONFIG["password_selector"], password)
    page.click(CONFIG["submit_selector"])

    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=timeout)
    except PWTimeout:
        _screenshot(page, "login_failed")
        if CONFIG["error_selector"] and page.locator(CONFIG["error_selector"]).count():
            msg = page.locator(CONFIG["error_selector"]).first.inner_text()
            raise LoginError(f"Login rejected by Nexus: {msg.strip()}")
        raise LoginError("Login did not complete (wrong ID/password).")

    page.goto(CONFIG["orders_url"], wait_until="domcontentloaded")
    try:
        page.wait_for_selector(
            CONFIG["logged_in_selector"], state="visible", timeout=timeout
        )
    except PWTimeout:
        _screenshot(page, "orders_page_failed")
        raise OrdersPageError(
            "Logged in, but the Orders page did not open "
            f"(ended on: {page.url}). The account may have no access to Orders."
        )


def _launch_args() -> list:
    args = ["--disable-dev-shm-usage"]          # for Docker
    if os.getenv("NEXUS_NO_SANDBOX") == "1":
        args.append("--no-sandbox")
    return args


def get_logged_in_context(p, headless: bool = True):
    """
    Returns (browser, context, page), logged in with NEXUS_USERNAME / NEXUS_PASSWORD.
    Always a fresh login (no saved session).
    """
    username = os.getenv("NEXUS_USERNAME")
    password = os.getenv("NEXUS_PASSWORD")
    if not username or not password:
        raise LoginError("Nexus ID / password missing.")

    browser = p.chromium.launch(headless=headless, args=_launch_args())
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(CONFIG["timeout_ms"])
    try:
        login(page, username, password)
    except Exception:
        browser.close()
        raise

    print("[ok] Logged in.")
    return browser, context, page


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    with sync_playwright() as p:
        try:
            browser, context, page = get_logged_in_context(p, headless=headless)
        except LoginError as e:
            print(f"[error] {e}")
            sys.exit(1)

        print(f"[ok] Current page: {page.url}")
        if not headless:
            input("Press Enter to close the browser...")
        browser.close()