import base64
import sys
import time
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from nexus_login import LoginError, get_logged_in_context, screenshot
from nexus_search import search_po
from nexus_order import open_order

PDF = {
    "more_button": "button.btn-actions[data-buttonname='$menu.$more']",
    "view_pdf": "a[data-buttonname='viewPDF']",
    "timeout_ms": 30_000,
}

PDF_DIR = Path("output/pdfs")


def _read_pdf_from_url(page: Page, popup: Page) -> bytes | None:
    """Fallback: if a window stayed open with a PDF address, fetch it."""
    try:
        url = popup.url
        if not url or url == "about:blank":
            return None
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


def download_pdf(page: Page, po_id: str) -> dict:
    """
    Call this while the browser is on the order details page.
    Saves output/pdfs/<po_id>.pdf
    Returns {"po": ..., "status": "downloaded" | "error", "path": ...}
    """
    po_id = po_id.strip()
    timeout = PDF["timeout_ms"]
    context = page.context
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{po_id}.pdf"          # file is named with the PO id

    downloads, new_pages = [], []

    def on_download(d):
        downloads.append(d)

    def on_new_page(pg):
        new_pages.append(pg)
        pg.on("download", on_download)       # downloads started in the new window

    page.on("download", on_download)
    context.on("page", on_new_page)

    try:
        # open the three-dot menu
        page.locator(PDF["more_button"]).first.click(timeout=timeout)

        # click View PDF
        view = page.locator(PDF["view_pdf"]).first
        view.wait_for(state="visible", timeout=timeout)
        view.click()

        # wait for the download to start
        deadline = time.time() + timeout / 1000
        while not downloads and time.time() < deadline:
            page.wait_for_timeout(200)

        # normal case: a download was caught
        if downloads:
            downloads[0].save_as(str(path))
            if path.read_bytes()[:4] == b"%PDF":
                return {"po": po_id, "status": "downloaded", "method": "download",
                        "path": str(path)}
            path.unlink(missing_ok=True)
            return {"po": po_id, "status": "error",
                    "note": "Downloaded file is not a PDF."}

        # fallback: a window stayed open showing the PDF
        for pg in new_pages:
            data = _read_pdf_from_url(page, pg)
            if data and data.startswith(b"%PDF"):
                path.write_bytes(data)
                return {"po": po_id, "status": "downloaded", "method": "window url",
                        "path": str(path)}

        screenshot(page, f"pdf_{po_id}_no_download")
        return {"po": po_id, "status": "error",
                "note": f"No download started. New windows seen: {len(new_pages)}"}

    except PWTimeout:
        screenshot(page, f"pdf_{po_id}_timeout")
        return {"po": po_id, "status": "error",
                "note": "Timed out (three-dot menu or View PDF button not found)."}
    except Exception as e:
        screenshot(page, f"pdf_{po_id}_exception")
        return {"po": po_id, "status": "error", "note": f"{type(e).__name__}: {e}"}
    finally:
        page.remove_listener("download", on_download)
        context.remove_listener("page", on_new_page)


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
        browser.close() #python automation_stage/nexus_pdf.py 62397850
 