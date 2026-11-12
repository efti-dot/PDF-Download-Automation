import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Nexus PO Downloader", page_icon="📄")

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
WORKER = APP_DIR / "nexus_worker.py"

MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "3"))
MAX_POS_PER_RUN = int(os.getenv("MAX_POS_PER_RUN", "200"))
MAX_RUN_MINUTES = int(os.getenv("MAX_RUN_MINUTES", "240"))
IDLE_LOGOUT_MINUTES = int(os.getenv("IDLE_LOGOUT_MINUTES", "15"))
MAX_FAILED_LOGINS = 3
FAIL_WINDOW_SECONDS = 15 * 60
COOKIE_NAME = "nexus_sid"

KNOWN_PO_HEADERS = {"po", "po number", "po no", "po id", "po #", "po_number",
                    "order", "order number", "purchase order"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
log = logging.getLogger("nexus_app")


# shared state for all users
@st.cache_resource
def shared():
    return {
        "sem": threading.BoundedSemaphore(MAX_CONCURRENT_RUNS),
        "fails": {},
        "sessions": {},          # token
        "lock": threading.Lock(),
    }


@contextmanager
def run_slot():
    """Yields True if a browser slot is free, False if the server is busy."""
    sem = shared()["sem"]
    got = sem.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            sem.release()


def recent_failures(user: str) -> int:
    s, now = shared(), time.time()
    with s["lock"]:
        times = [t for t in s["fails"].get(user.lower(), []) if now - t < FAIL_WINDOW_SECONDS]
        s["fails"][user.lower()] = times
        return len(times)


def record_failure(user: str) -> None:
    s = shared()
    with s["lock"]:
        s["fails"].setdefault(user.lower(), []).append(time.time())


def clear_failures(user: str) -> None:
    s = shared()
    with s["lock"]:
        s["fails"].pop(user.lower(), None)


# login sessions save

def create_session(user: str, pw: str) -> str:
    token = secrets.token_urlsafe(32)
    s = shared()
    with s["lock"]:
        s["sessions"][token] = {"user": user, "pw": pw, "last": time.time()}
    return token


def get_session(token):
    """Returns the session (and counts as activity), or None if missing / idle too long."""
    if not token:
        return None
    s, now = shared(), time.time()
    with s["lock"]:
        for t in [t for t, v in s["sessions"].items()
                  if now - v["last"] > IDLE_LOGOUT_MINUTES * 60]:
            del s["sessions"][t]
        sess = s["sessions"].get(token)
        if sess:
            sess["last"] = now
            return dict(sess)
        return None


def drop_session(token) -> None:
    s = shared()
    with s["lock"]:
        s["sessions"].pop(token, None)


def set_cookie(name: str, value: str, delete: bool = False) -> None:
    """Session cookie (gone when the browser closes). Secure flag is added on https."""
    age = "; max-age=0" if delete else ""
    components.html(
        f"""<script>
        const secure = window.parent.location.protocol === 'https:' ? '; Secure' : '';
        window.parent.document.cookie = "{name}={value}; path=/; SameSite=Strict" + secure + "{age}";
        </script>""",
        height=0,
    )


# login check
def verify_login(user: str, pw: str):
    """Returns (ok, kind, message). Runs the worker in --check-login mode."""
    with tempfile.TemporaryDirectory(prefix="nexus_chk_") as shots:
        env = {**os.environ, "NEXUS_USERNAME": user, "NEXUS_PASSWORD": pw,
               "NEXUS_SCREENSHOT_DIR": shots}
        try:
            proc = subprocess.run(
                [sys.executable, str(WORKER), "--check-login"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except subprocess.TimeoutExpired:
            return False, "system", "Nexus did not answer in time. Please try again."

    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("RESULT_JSON:"):
            d = json.loads(line[len("RESULT_JSON:"):])
            if d.get("ok"):
                return True, "", ""
            if d.get("kind") == "login":
                return False, "login", "Login failed. Check your Nexus ID and password."
            if d.get("kind") == "orders":
                log.info("orders page not available: %s", d.get("error"))
                return False, "orders", ("Your login works, but the Orders page could not be "
                                         "opened for this account.")
            log.error("check-login system error: %s", d.get("error"))
            return False, "system", "Could not check the login (server problem)."
    log.error("check-login worker gave no result")
    return False, "system", "Could not check the login (server problem)."


def sign_in_screen() -> None:
    st.title("Nexus PO PDF Downloader")
    st.caption("Sign in with your own Infor Nexus ID and password.")
    with st.form("signin"):
        user = st.text_input("Nexus ID")
        pw = st.text_input("Password", type="password")
        go = st.form_submit_button("Login", type="primary")
    if not go:
        return

    user = user.strip()
    if not user or not pw:
        st.error("Enter both your Nexus ID and password.")
        return
    if recent_failures(user) >= MAX_FAILED_LOGINS:
        st.error("Too many failed attempts for this ID. Please wait 15 minutes, "
                 "and check the password directly on the Nexus website.")
        return

    with run_slot() as ok:
        if not ok:
            st.warning("The server is busy right now. Please try again in a few minutes.")
            return
        with st.spinner("Checking your Nexus login..."):
            valid, kind, msg = verify_login(user, pw)

    if valid:
        clear_failures(user)
        token = create_session(user, pw)
        st.session_state.update(user=user, pw=pw, token=token, set_cookie=token)
        log.info("login ok user=%s", user)
        st.rerun()
    else:
        if kind == "login":
            record_failure(user)
            log.info("login failed user=%s", user)
        st.error(msg)


# excel helpers
def clean_po(value) -> str:
    s = str(value).strip()
    if s.lower() == "nan":
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    return s


def load_table(uploaded) -> pd.DataFrame:
    if uploaded.name.lower().endswith(".csv"):
        return pd.read_csv(uploaded, dtype=str)
    return pd.read_excel(uploaded, dtype=str)


def rows_to_df(rows: list) -> pd.DataFrame:
    return pd.DataFrame([
        {"PO": r.get("po"), "Status": r.get("status"),
         "Details": Path(r["path"]).name if r.get("path") else r.get("note", "")}
        for r in rows
    ])


def run_batch(po_list: list, user: str, pw: str, token: str) -> dict:
    """Run the worker in a private temp folder, show live progress, return rows + ZIP."""
    work = Path(tempfile.mkdtemp(prefix="nexus_run_"))
    pdf_dir, shots = work / "pdfs", work / "shots"
    po_file = work / "pos.txt"
    po_file.write_text("\n".join(po_list), encoding="utf-8")

    env = {**os.environ, "NEXUS_USERNAME": user, "NEXUS_PASSWORD": pw,
           "NEXUS_SCREENSHOT_DIR": str(shots)}
    cmd = [sys.executable, "-u", str(WORKER), "--po-file", str(po_file),
           "--out", str(pdf_dir), "--headless"]

    bar = st.progress(0.0, text="Starting browser and logging in...")
    table_box = st.empty()
    rows, log_lines, final, zip_bytes = [], [], None, None
    timed_out = threading.Event()

    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    def _kill():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(MAX_RUN_MINUTES * 60, _kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            log_lines.append(line)
            if line.startswith("PROGRESS:"):
                d = json.loads(line[len("PROGRESS:"):])
                rows.append(d["result"])
                get_session(token)        # long runs count as activity (no idle logout)
                bar.progress(d["index"] / d["total"],
                             text=f"{d['index']} of {d['total']} done")
                table_box.dataframe(rows_to_df(rows))
            elif line.startswith("RESULT_JSON:"):
                final = json.loads(line[len("RESULT_JSON:"):])
        proc.wait()
        get_session(token)

        if pdf_dir.exists() and any(pdf_dir.glob("*.pdf")):
            archive = shutil.make_archive(str(work / "nexus_pdfs"), "zip", pdf_dir)
            zip_bytes = Path(archive).read_bytes()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)   # all files deleted

    bar.empty()
    table_box.empty()

    counts = {}
    for r in rows:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    log.info("run user=%s pos=%d result=%s timed_out=%s",
             user, len(po_list), counts, timed_out.is_set())

    return {"rows": rows, "final": final, "zip": zip_bytes, "timed_out": timed_out.is_set(),
            "log": "\n".join(log_lines)}


# session restore
if "user" in st.session_state:
    if not get_session(st.session_state.get("token")):     # idle too long
        st.session_state.clear()
        st.session_state["notice"] = "You were signed out after a period of inactivity."
else:
    cookie_token = st.context.cookies.get(COOKIE_NAME)
    sess = get_session(cookie_token)
    if sess:
        st.session_state.update(user=sess["user"], pw=sess["pw"], token=cookie_token)

if st.session_state.pop("clear_cookie", False):
    set_cookie(COOKIE_NAME, "", delete=True)

if "user" not in st.session_state:
    notice = st.session_state.pop("notice", None)
    if notice:
        st.info(notice)
    sign_in_screen()
    st.stop()

pending_cookie = st.session_state.pop("set_cookie", None)
if pending_cookie:
    set_cookie(COOKIE_NAME, pending_cookie)

user = st.session_state["user"]
pw = st.session_state["pw"]

with st.sidebar:
    st.write(f"Signed in as **{user}**")
    if st.button("Log out"):
        drop_session(st.session_state.get("token"))
        st.session_state.clear()          # removes all
        st.session_state["clear_cookie"] = True
        st.rerun()

# main page
st.title("Nexus PO PDF Downloader")

po_typed = st.text_input("PO number", placeholder="").strip()
uploaded = st.file_uploader("Upload an Excel file",
                            type=["xlsx", "csv"])

excel_pos, invalid = [], []
if uploaded is not None:
    try:
        df = load_table(uploaded)
        cols = list(df.columns)
        default = next((i for i, c in enumerate(cols)
                        if str(c).strip().lower() in KNOWN_PO_HEADERS), 0)
        col = st.selectbox("Which column has the PO numbers?", cols, index=default)
        for v in df[col].tolist():
            s = clean_po(v)
            if not s:
                continue
            (excel_pos if s.isdigit() else invalid).append(s)
    except Exception as e:
        st.error(f"Could not read the file: {e}")

if po_typed:
    if po_typed.isdigit():
        excel_pos.insert(0, po_typed)
    else:
        invalid.append(po_typed)

po_list = list(dict.fromkeys(excel_pos))          # remove duplicates, keep order

if po_list:
    st.info(f"{len(po_list)} PO number(s) ready (maximum {MAX_POS_PER_RUN} per run).")
if invalid:
    st.warning(f"Ignored {len(invalid)} value(s) that are not digits only: "
               f"{', '.join(invalid[:5])}{' ...' if len(invalid) > 5 else ''}")

if st.button("Start download", type="primary"):
    if not po_list:
        st.error("Enter a PO number or upload an Excel file first.")
    elif len(po_list) > MAX_POS_PER_RUN:
        st.error(f"Too many POs: {len(po_list)}. Please split the list into runs of "
                 f"{MAX_POS_PER_RUN} or fewer.")
    else:
        with run_slot() as ok:
            if not ok:
                st.warning("The server is busy with other downloads. "
                           "Please try again in a few minutes.")
            else:
                st.session_state["batch"] = run_batch(po_list, user, pw, st.session_state["token"])

batch = st.session_state.get("batch")
if batch:
    final, rows = batch["final"], batch["rows"]

    if batch["timed_out"]:
        st.warning(f"The run was stopped after {MAX_RUN_MINUTES} minutes. "
                   "The PDFs finished so far are in the ZIP. Run the rest again.")

    if final is None and not rows:
        st.error("The download stopped before it started. Please try again.")
    elif final and final.get("login_error"):
        st.error("Login to Nexus failed. Please log out and sign in again.")
    else:
        df_res = rows_to_df(rows)
        counts = df_res["Status"].value_counts().to_dict() if len(df_res) else {}
        st.success(
            f"Finished. Downloaded: {counts.get('downloaded', 0)} | "
            f"Not found: {counts.get('not_found', 0)} | "
            f"Errors: {counts.get('error', 0) + counts.get('login_failed', 0)}"
        )
        st.dataframe(df_res)

        c1, c2 = st.columns(2)
        if batch["zip"]:
            c1.download_button("Download all PDFs (ZIP)", batch["zip"],
                               file_name="nexus_pdfs.zip", mime="application/zip",
                               type="primary")
        c2.download_button("Report (CSV)", df_res.to_csv(index=False).encode("utf-8"),
                           file_name="nexus_download_report.csv", mime="text/csv")

    with st.expander("Details / log"):
        st.code(batch["log"] or "(no log)")