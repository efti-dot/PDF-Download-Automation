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


class SessionExpiredError(LoginError):
    """The saved Nexus session ended and there is no password to log in again
    (the app keeps cookies only). The user must sign in to the app again."""


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
    Raises SessionExpiredError without touching Nexus if there is no password,
    so an empty password is never sent (it could count as a failed login).
    """
    if not username or not password:
        raise SessionExpiredError("The Nexus session has ended. Please sign in again.")
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


def _state_file():
    """Saved Nexus session (cookies) file, set by the app with NEXUS_STATE_FILE."""
    path = os.getenv("NEXUS_STATE_FILE")
    return Path(path) if path else None


def save_session(context) -> None:
    """Save the Nexus session cookies so the next run can skip the login."""
    state = _state_file()
    if not state:
        return
    try:
        context.storage_state(path=str(state))
    except Exception as e:
        print(f"[warn] could not save the Nexus session: {type(e).__name__}")


def forget_session() -> None:
    """Delete the saved session file (after a login problem)."""
    state = _state_file()
    if state:
        state.unlink(missing_ok=True)


def _reuse_session(browser, state: Path):
    """Opens the Orders page with the saved cookies. Returns (context, page) or None."""
    context = None
    try:
        context = browser.new_context(accept_downloads=True, storage_state=str(state))
        page = context.new_page()
        page.set_default_timeout(CONFIG["timeout_ms"])
        page.goto(CONFIG["orders_url"], wait_until="domcontentloaded")
        if is_logged_in(page, CONFIG["timeout_ms"]): 
            return context, page
    except Exception:
        pass 
    if context:
        context.close()
    return None


def get_logged_in_context(p, headless: bool = True):
    """
    Returns (browser, context, page), logged in to Nexus.
    1) If NEXUS_STATE_FILE points to a saved session that is still valid, it is reused.
    2) Otherwise, if NEXUS_PASSWORD is set, a fresh login is done (app sign-in check,
       or the stand-alone scripts with a .env file).
    3) Otherwise SessionExpiredError (the app keeps cookies only, never the password).
    """
    username = os.getenv("NEXUS_USERNAME")
    password = os.getenv("NEXUS_PASSWORD")
    state = _state_file()
    has_state = bool(state and state.exists())
    if not username or (not password and not has_state):
        raise SessionExpiredError("No Nexus session or password. Please sign in again.")

    browser = p.chromium.launch(headless=headless, args=_launch_args())

    if has_state:
        reused = _reuse_session(browser, state)
        if reused:
            print("[ok] Reused the saved Nexus session (no new login).")
            return (browser, *reused)
        if not password:
            browser.close()
            raise SessionExpiredError("The Nexus session has ended. Please sign in again.")
        print("[info] Saved Nexus session expired, logging in again.")

    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(CONFIG["timeout_ms"])
    try:
        login(page, username, password)
    except Exception:
        browser.close()
        raise

    print("[ok] Logged in.")
    save_session(context)
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

