import sys

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from nexus_login import LoginError, get_logged_in_context, _screenshot as screenshot
from nexus_search import SEARCH, search_po

ORDER = {
    "timeout_ms": 20_000,
}


def open_order(page: Page, po_id: str) -> dict:
    """
    Clicks the PO id link in the matching result row and waits for the
    order details page to load. Call this AFTER search_po() returned 'found'.
    Returns {"po": ..., "status": "opened" | "error", "url": ...}
    """
    po_id = po_id.strip()
    timeout = ORDER["timeout_ms"]

    row = page.locator(SEARCH["result_row_selector"]).filter(has_text=po_id).first

    # 1) Preferred: the link whose text is exactly the PO id (the purple link).
    #    exact=True avoids the "Contract Ref / ID" link ("62397850-Packing PO-...").
    link = row.get_by_role("link", name=po_id, exact=True).first
    # 2) Fallback: any link in the row that contains the PO id
    if link.count() == 0:
        link = row.locator("a", has_text=po_id).first

    old_url = page.url
    link.click()

    try:
        page.wait_for_url(lambda url: url != old_url, timeout=timeout)
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PWTimeout:
        screenshot(page, f"order_{po_id}_not_opened")
        return {
            "po": po_id,
            "status": "error",
            "url": page.url,
            "note": "Page did not change. The link may open in a new tab or popup.",
        }

    return {"po": po_id, "status": "opened", "url": page.url}


if __name__ == "__main__":
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
        print(f"[search] {result}")

        if result["status"] == "found":
            result = open_order(page, po_id)
            print(f"[order] {result}")
        else:
            print("[stop] PO not found, nothing to open.")

        if not headless:
            input("Press Enter to close the browser...")
        browser.close() #python app/nexus_order.py 62397850