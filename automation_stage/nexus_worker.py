import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

import nexus_pdf
from nexus_login import (CONFIG, LoginError, OrdersPageError,
                         get_logged_in_context, is_logged_in, login)
from nexus_order import open_order
from nexus_pdf import download_pdf
from nexus_search import search_po

PAUSE_BETWEEN_POS_MS = 300      # small pause
MAX_ATTEMPTS = 2                # 1 try + 1 retry for error


def emit(kind: str, payload) -> None:
    print(f"{kind}:" + json.dumps(payload), flush=True)


# check login
def check_login() -> dict:
    try:
        with sync_playwright() as p:
            try:
                browser, context, page = get_logged_in_context(p, headless=True)
            except LoginError as e:
                kind = "orders" if isinstance(e, OrdersPageError) else "login"
                return {"ok": False, "kind": kind, "error": str(e)}
            browser.close()
            return {"ok": True}
    except Exception as e:
        return {"ok": False, "kind": "system", "error": f"{type(e).__name__}: {e}"}


# download PDFs
def go_home(page, home_url: str) -> None:
    """Return to the orders page (with the PO filter box). Re-login if the session expired."""
    page.goto(home_url, wait_until="domcontentloaded")
    if not is_logged_in(page):
        login(page, os.getenv("NEXUS_USERNAME", ""), os.getenv("NEXUS_PASSWORD", ""))


def process_one(page, po_id: str, home_url: str) -> dict:
    go_home(page, home_url)

    found = search_po(page, po_id)
    if found["status"] != "found":
        return {"po": po_id, "status": "not_found",
                "note": "PO was not found in the Nexus search."}

    opened = open_order(page, po_id)
    if opened["status"] != "opened":
        return {"po": po_id, "status": "error",
                "note": opened.get("note", "Could not open the order page.")}

    return download_pdf(page, po_id)


def run(po_ids: list, out_dir: str, headless: bool, skip_existing: bool) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nexus_pdf.PDF_DIR = out          # tell nexus_pdf where to save

    results, total = [], len(po_ids)

    with sync_playwright() as p:
        try:
            browser, context, page = get_logged_in_context(p, headless=headless)
        except LoginError as e:
            return {"login_error": str(e), "results": []}

        home_url = CONFIG["orders_url"]   # the Orders page return
        aborted = False

        try:
            for i, po in enumerate(po_ids, 1):
                target = out / f"{po}.pdf"

                if skip_existing and target.exists():
                    res = {"po": po, "status": "skipped",
                           "note": "File already exists.", "path": str(target)}
                else:
                    res = None
                    for attempt in range(MAX_ATTEMPTS):
                        try:
                            res = process_one(page, po, home_url)
                        except LoginError as e:
                            res = {"po": po, "status": "login_failed", "note": str(e)}
                            aborted = True
                            break
                        except Exception as e:
                            res = {"po": po, "status": "error",
                                   "note": f"{type(e).__name__}: {e}"}
                        if res["status"] != "error":
                            break
                    page.wait_for_timeout(PAUSE_BETWEEN_POS_MS)

                results.append(res)
                emit("PROGRESS", {"index": i, "total": total, "result": res})
                if aborted:
                    break
        finally:
            browser.close()

    return {"login_error": None, "results": results}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("po_ids", nargs="*", help="one or more PO numbers")
    ap.add_argument("--po-file", help="text file, one PO number per line")
    ap.add_argument("--out", help="folder to save the PDFs in")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--check-login", action="store_true")
    args = ap.parse_args()

    if args.check_login:
        emit("RESULT_JSON", check_login())
        raise SystemExit(0)

    if not args.out:
        ap.error("--out is required")

    pos = [p.strip() for p in args.po_ids if p.strip()]
    if args.po_file:
        pos += [l.strip() for l in Path(args.po_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    pos = list(dict.fromkeys(pos))     # remove duplicates, keep order

    emit("RESULT_JSON", run(pos, args.out, args.headless, args.skip_existing))