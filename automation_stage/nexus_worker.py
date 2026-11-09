import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import nexus_pdf
from nexus_login import LoginError, get_logged_in_context
from nexus_order import open_order
from nexus_pdf import download_pdf
from nexus_search import search_po


def run(po_id: str, out_dir: str, headless: bool) -> dict:
    # Tell nexus_pdf where to save
    nexus_pdf.PDF_DIR = Path(out_dir)

    with sync_playwright() as p:
        try:
            browser, context, page = get_logged_in_context(p, headless=headless)
        except LoginError as e:
            return {"po": po_id, "status": "login_failed", "note": str(e)}

        try:
            found = search_po(page, po_id)
            if found["status"] != "found":
                return {"po": po_id, "status": "not_found",
                        "note": "PO was not found in the Nexus search."}

            opened = open_order(page, po_id)
            if opened["status"] != "opened":
                return {"po": po_id, "status": "error",
                        "note": opened.get("note", "Could not open the order page.")}

            return download_pdf(page, po_id)
        except Exception as e:
            return {"po": po_id, "status": "error", "note": f"{type(e).__name__}: {e}"}
        finally:
            browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("po_id")
    ap.add_argument("--out", required=True, help="folder to save the PDF in")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    result = run(args.po_id.strip(), args.out, args.headless)
    print("RESULT_JSON:" + json.dumps(result))