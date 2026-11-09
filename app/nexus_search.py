import sys

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from nexus_login import LoginError, get_logged_in_context, _screenshot as screenshot

SEARCH = {
    "po_input_selector": "input[name='ordersFilterOrder']",
    "apply_selector": "input.styledActionButton[value='Apply']",
    "result_row_selector": "tr[class*='listtablerow']",
    "no_result_selector": "",
    "timeout_ms": 20_000,
}


def _check_config() -> None:
    missing = [k for k, v in SEARCH.items() if isinstance(v, str) and "REPLACE" in v]
    if missing:
        raise SystemExit(f"[error] Fill these in SEARCH first: {', '.join(missing)}")


def search_po(page: Page, po_id: str) -> dict:
    """
    Types the PO id, clicks Apply, waits for a result row that contains the PO id.
    Returns {"po": ..., "status": "found" | "not_found", "rows": n}
    """
    po_id = po_id.strip()
    timeout = SEARCH["timeout_ms"]

    page.wait_for_selector(SEARCH["po_input_selector"], state="visible", timeout=timeout)
    page.fill(SEARCH["po_input_selector"], po_id)   # fill() replaces any old value
    page.click(SEARCH["apply_selector"])

    rows = page.locator(SEARCH["result_row_selector"]).filter(has_text=po_id)
    try:
        rows.first.wait_for(state="visible", timeout=timeout)
    except PWTimeout:
        if SEARCH["no_result_selector"] and page.locator(SEARCH["no_result_selector"]).count():
            return {"po": po_id, "status": "not_found", "rows": 0}
        screenshot(page, f"search_{po_id}_timeout")
        return {"po": po_id, "status": "not_found", "rows": 0}

    return {"po": po_id, "status": "found", "rows": rows.count()}


if __name__ == "__main__":
    _check_config()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    po_id = args[0] if args else input("PO id: ")
    headless = "--headless" in sys.argv

    with sync_playwright() as p:
        try:
            browser, context, page = get_logged_in_context(p, headless=headless)
        except LoginError as e:
            print(f"[error] {e}")
            sys.exit(1)

        result = search_po(page, po_id)
        print(f"[result] {result}")

        if not headless:
            input("Press Enter to close the browser...")
        browser.close() #python app/nexus_search.py 62374860