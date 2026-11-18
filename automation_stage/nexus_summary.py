import os
import re
import sys

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from nexus_login import LoginError, get_logged_in_context, login, screenshot

SUMMARY = {
    "manifests_url": "https://network.infornexus.com/en/trade/PackingPlanManifestsView.jsp",
    "po_input_selector": "input[name='InProgressManifestsPageManagerpoNumberFilter']",
    "apply_selector": "input.styledActionButton[value='Apply']",
    "result_link_selector": ("td.listtablecell a[href*='ShipmentManifestPageResolver']"
                             "[href*='destination=PackingPlanManifest']"),
    "summary_header_selector": "td.reviewtableinnerheadercellsmall",
    "timeout_ms": 20_000,
}

# Excel columns
TOTAL_COLUMNS = ["Item Qty", "Pkg Count", "Net Net", "Net", "Gross",
                 "Weight Unit", "Vol", "Vol Unit"]


_TOTALS_JS = """
() => {
    const heads = [...document.querySelectorAll('td.reviewtableinnerheadercellsmall')];
    const itemQty = heads.find(td => td.innerText.trim() === 'Item Qty');
    if (!itemQty) return null;
    const headerRow = itemQty.closest('tr');
    const table = headerRow.closest('table');
    const headers = [...headerRow.cells].map(c => c.innerText.trim());
    for (const row of table.rows) {
        const cells = [...row.cells].map(c => c.innerText.trim());
        const i = cells.indexOf('Totals');
        if (i >= 0) {
            return {headers: headers.slice(headers.indexOf('Item Qty')),
                    values: cells.slice(i + 1)};
        }
    }
    return {headers: headers, values: null};
}
"""


class ManifestsPageError(Exception):
    """The Packing Plan Manifests page (with the PO filter box) could not be opened."""


def _filter_visible(page: Page, timeout_ms: int) -> bool:
    try:
        page.wait_for_selector(SUMMARY["po_input_selector"], state="visible", timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


def open_manifests_page(page: Page) -> None:
    """Go to the manifests page. If the session ended: logs in again when a password
    is available, otherwise login() raises SessionExpiredError."""
    page.goto(SUMMARY["manifests_url"], wait_until="domcontentloaded")
    if _filter_visible(page, SUMMARY["timeout_ms"]):     # long wait
        return

    login(page, os.getenv("NEXUS_USERNAME", ""), os.getenv("NEXUS_PASSWORD", ""))
    page.goto(SUMMARY["manifests_url"], wait_until="domcontentloaded")
    if _filter_visible(page, SUMMARY["timeout_ms"]):
        return

    screenshot(page, "manifests_page_failed")
    raise ManifestsPageError(
        "The Packing Plan Manifests page did not open "
        f"(ended on: {page.url}). The account may have no access to it."
    )


def _apply_button(page: Page):
    """The Apply button in the same form as the PO box (the page may have several)."""
    form = page.locator("form").filter(has=page.locator(SUMMARY["po_input_selector"]))
    scope = form.first if form.count() else page
    return scope.locator(SUMMARY["apply_selector"]).first


def _pick_link(links, po_id: str):
    """Best result link for this PO: link text == PO, else a row that shows the PO."""
    n = links.count()
    for i in range(n):
        if links.nth(i).inner_text().strip() == po_id:
            return links.nth(i), n
    po_alone = re.compile(rf"(?<!\d){re.escape(po_id)}(?!\d)")   # not part of a longer number
    for i in range(n):
        row_text = links.nth(i).evaluate("a => (a.closest('tr') || a).innerText")
        if po_alone.search(row_text or ""):
            return links.nth(i), n
    return None, n


def open_po_summary(page: Page, po_id: str) -> dict:
    """
    Call this while on the manifests page (see open_manifests_page).
    Returns {"po", "status": "opened" | "not_found" | "error", "url", "manifest", "note"}
    """
    po_id = po_id.strip()
    timeout = SUMMARY["timeout_ms"]

    page.locator(SUMMARY["po_input_selector"]).first.fill(po_id)

    reloaded = True
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout):
            _apply_button(page).click()
    except PWTimeout:
        reloaded = False          # the table may have refreshed without a page load

    links = page.locator(SUMMARY["result_link_selector"])
    try:
        links.first.wait_for(state="visible", timeout=timeout)
    except PWTimeout:
        screenshot(page, f"summary_{po_id}_no_result")
        return {"po": po_id, "status": "not_found",
                "note": "No result for this PO on the Packing Plan Manifests page."}

    link, n = _pick_link(links, po_id)
    if link is None:
        if not reloaded:
            screenshot(page, f"summary_{po_id}_not_filtered")
            return {"po": po_id, "status": "not_found",
                    "note": "The result table did not refresh for this PO."}
        link = links.first

    manifest = link.inner_text().strip()
    old_url = page.url
    link.click()
    try:
        page.wait_for_url(lambda u: u != old_url, timeout=timeout)
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PWTimeout:
        screenshot(page, f"summary_{po_id}_not_opened")
        return {"po": po_id, "status": "error", "url": page.url,
                "note": "Clicked the result, but the PO summary page did not open."}

    note = f"Opened summary {manifest}"
    if n > 1:
        note += f" (first of {n} results)"
    return {"po": po_id, "status": "opened", "url": page.url,
            "manifest": manifest, "results": n, "note": note}


def _num(text: str):
    """'1,234' -> 1234, '11.02' -> 11.02, 'KG' -> 'KG'."""
    s = text.replace(",", "").strip()
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        return text.strip()


def read_summary_totals(page: Page, po_id: str, opened: dict | None = None) -> dict:
    """
    Call this on the PO summary page (after open_po_summary).
    Returns {"po", "status": "done" | "error", "totals": {column: value}, "note"}
    """
    po_id = po_id.strip()
    opened = opened or {}
    try:
        page.locator(SUMMARY["summary_header_selector"], has_text="Item Qty").first.wait_for(
            state="visible", timeout=SUMMARY["timeout_ms"])
    except PWTimeout:
        screenshot(page, f"summary_{po_id}_no_table")
        return {"po": po_id, "status": "error", "url": page.url,
                "note": "PO Summary table not found on the page."}

    data = page.evaluate(_TOTALS_JS)
    if not data or not data.get("values"):
        screenshot(page, f"summary_{po_id}_no_totals")
        return {"po": po_id, "status": "error", "url": page.url,
                "note": "PO Summary table found, but no 'Totals' row."}

    values = data["values"][:len(TOTAL_COLUMNS)]
    if len(values) < len(TOTAL_COLUMNS):
        screenshot(page, f"summary_{po_id}_short_totals")
        return {"po": po_id, "status": "error", "url": page.url,
                "note": f"Totals row has {len(values)} values, expected {len(TOTAL_COLUMNS)}."}

    totals = {col: _num(v) for col, v in zip(TOTAL_COLUMNS, values)}
    note = (f"Qty {totals['Item Qty']}, Pkgs {totals['Pkg Count']}, "
            f"Gross {totals['Gross']} {totals['Weight Unit']}, "
            f"Vol {totals['Vol']} {totals['Vol Unit']}")
    if opened.get("results", 1) > 1:
        note += f" (first of {opened['results']} results)"
    return {"po": po_id, "status": "done", "url": page.url,
            "manifest": opened.get("manifest"), "totals": totals, "note": note}


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

        try:
            open_manifests_page(page)
            result = open_po_summary(page, po_id)
            if result["status"] == "opened":
                result = read_summary_totals(page, po_id, result)
        except (ManifestsPageError, LoginError) as e:
            result = {"po": po_id, "status": "error", "note": str(e)}
        print(f"[summary] {result}")

        if not headless:
            input("Press Enter to close the browser...")
        browser.close()   # python automation_stage/nexus_summary.py 62374860
