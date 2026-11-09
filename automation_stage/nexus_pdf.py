import base64
import sys
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from nexus_login import LoginError, get_logged_in_context, _screenshot as screenshot
from nexus_search import SEARCH, search_po
from nexus_order import open_order

PDF = {
    "more_button": "button.btn-actions[data-buttonname='$menu.$more']",
    "view_pdf": "a[data-buttonname='viewPDF']",
    "viewer_download": "#save",
    "timeout_ms": 30_000,
}
 
PDF_DIR = Path("output/pdfs")
 
 
def _read_pdf_from_url(page: Page, popup: Page) -> bytes | None:
    """Fetch the PDF bytes from the popup's URL using the logged-in session."""
    url = popup.url
    try:
        if url.startswith("blob:"):
            b64 = popup.evaluate(
                """async (u) => {
                    const r = await fetch(u);
                    const bytes = new Uint8Array(await r.arrayBuffer());
                    let s = '';
                    bytes.forEach(b => s += String.fromCharCode(b));
                    return btoa(s);
                }""",
                url,
            )
            return base64.b64decode(b64)
        return page.context.request.get(url).body()
    except Exception:
        return None
 
 
def _click_viewer_download(popup: Page, path: Path) -> bool:
    """Fallback: click the Download button of Chrome's PDF viewer and save as <path>."""
    try:
        with popup.expect_download(timeout=10_000) as dl:
            popup.locator(PDF["viewer_download"]).first.click(timeout=5_000)
        dl.value.save_as(str(path))
        return True
    except Exception:
        return False
 
 
def download_pdf(page: Page, po_id: str) -> dict:
    """
    Call this while the browser is on the order details page.
    Saves output/pdfs/<po_id>.pdf
    Returns {"po": ..., "status": "downloaded" | "error", "path": ...}
    """
    po_id = po_id.strip()
    timeout = PDF["timeout_ms"]
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{po_id}.pdf"          # file is named with the PO id
 
    try:
        # open the three-dot menu
        page.locator(PDF["more_button"]).first.click(timeout=timeout)
 
        # click View PDF and catch the new window it opens
        view = page.locator(PDF["view_pdf"]).first
        view.wait_for(state="visible", timeout=timeout)
        with page.expect_popup(timeout=timeout) as popup_info:
            view.click()
        popup = popup_info.value
 
        # wait until the new window has a real URL (not about:blank)
        popup.wait_for_url(lambda u: u != "about:blank", timeout=timeout)
    except PWTimeout:
        screenshot(page, f"pdf_{po_id}_timeout")
        return {"po": po_id, "status": "error",
                "note": "Timed out (menu, View PDF button, or new window not found)."}
 
    # direct: read the PDF bytes from the window URL
    data = _read_pdf_from_url(page, popup)
    if data and data.startswith(b"%PDF"):
        path.write_bytes(data)
        popup.close()
        return {"po": po_id, "status": "downloaded", "method": "direct",
                "path": str(path)}
 
    # fallback: click the Download button in Chrome's PDF viewer
    if _click_viewer_download(popup, path):
        popup.close()
        return {"po": po_id, "status": "downloaded", "method": "viewer button",
                "path": str(path)}
 
    screenshot(popup, f"pdf_{po_id}_failed")
    return {"po": po_id, "status": "error", "url": popup.url,
            "note": "Could not read the PDF from the new window."}
 
 
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
            if result["status"] == "opened":
                result = download_pdf(page, po_id)
                print(f"[pdf] {result}")
        else:
            print("[stop] PO not found, nothing to download.")
 
        if not headless:
            input("Press Enter to close the browser...")
        browser.close() #python app/nexus_pdf.py 62397850