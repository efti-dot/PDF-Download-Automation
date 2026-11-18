import io
import json
import logging
import os
import re
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

st.set_page_config(page_title="Nexus Automation Tools", page_icon="📄")

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
WORKER = APP_DIR / "nexus_worker.py"

MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "5"))
MAX_POS_PER_RUN = int(os.getenv("MAX_POS_PER_RUN", "200"))
MAX_TYPED_POS = 10
MAX_RUN_MINUTES = int(os.getenv("MAX_RUN_MINUTES", "240"))
IDLE_LOGOUT_MINUTES = int(os.getenv("IDLE_LOGOUT_MINUTES", "15"))
MAX_FAILED_LOGINS = 3
FAIL_WINDOW_SECONDS = 15 * 60
COOKIE_NAME = "nexus_sid"

PAGE_PDF = "DPO PDF download"
PAGE_SUMMARY = "Packing Plans"

# PO summary Excel columns
SUMMARY_COLUMNS = ["PO", "Item Qty", "Pkg Count", "Net Net", "Net", "Gross",
                   "Weight Unit", "Vol", "Vol Unit"]

KNOWN_PO_HEADERS = {"po", "po number", "po no", "po id", "po #", "po_number",
                    "order", "order number", "purchase order"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
log = logging.getLogger("nexus_app")


TEMP_PREFIXES = ("nexus_run_", "nexus_chk_")     # private temp folders for cookies
SWEEP_SECONDS = 60


def remove_leftover_temp() -> None:
    """At start-up: delete temp folders left by a crash (could contain saved cookies).
    Assumes one app instance per server (it removes all such folders)."""
    tmp = Path(tempfile.gettempdir())
    removed = 0
    for prefix in TEMP_PREFIXES:
        for d in tmp.glob(prefix + "*"):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log.info("removed %d leftover temp folder(s)", removed)


def _sweeper(s: dict) -> None:
    """Every minute: remove idle sessions (and their cookies), even if nobody uses the app."""
    while True:
        time.sleep(SWEEP_SECONDS)
        with s["lock"]:
            n = _purge_idle(s, time.time())
        if n:
            log.info("idle logout: removed %d session(s)", n)


# shared state for all users
@st.cache_resource
def shared():
    s = {
        "sem": threading.BoundedSemaphore(MAX_CONCURRENT_RUNS),
        "fails": {},
        "sessions": {},
        "runs": {}, 
        "lock": threading.Lock(),
    }
    remove_leftover_temp()
    threading.Thread(target=_sweeper, args=(s,), daemon=True, name="session-sweeper").start()
    return s


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

def create_session(user: str, nexus_state: str) -> str:
    """nexus_state = saved Nexus cookies (JSON text), kept in memory only."""
    token = secrets.token_urlsafe(32)
    s = shared()
    with s["lock"]:
        s["sessions"][token] = {"user": user, "last": time.time(),
                                "nexus_state": nexus_state}
    return token


def set_nexus_state(token, nexus_state: str | None) -> None:
    """Update the saved Nexus cookies after a run (None = session ended, sign in again)."""
    s = shared()
    with s["lock"]:
        if token in s["sessions"]:
            s["sessions"][token]["nexus_state"] = nexus_state


def _drop(s: dict, token) -> None:
    """Remove a session and its saved Nexus cookies. Caller holds s["lock"].
    Also stops this user's running worker: it has its own copy of the cookies in its
    temp folder, which run_batch deletes when the worker ends."""
    s["sessions"].pop(token, None)
    for proc in s["runs"].pop(token, []):
        if proc.poll() is None:
            proc.kill()


def _purge_idle(s: dict, now: float) -> int:
    """Remove sessions idle longer than IDLE_LOGOUT_MINUTES. Caller holds s["lock"]."""
    idle = [t for t, v in s["sessions"].items()
            if now - v["last"] > IDLE_LOGOUT_MINUTES * 60]
    for t in idle:
        _drop(s, t)
    return len(idle)


def get_session(token):
    """Returns the session (and counts as activity), or None if missing / idle too long."""
    if not token:
        return None
    s, now = shared(), time.time()
    with s["lock"]:
        _purge_idle(s, now)
        sess = s["sessions"].get(token)
        if sess:
            sess["last"] = now
            return dict(sess)
        return None


def drop_session(token) -> None:
    """Log out: the session and its saved Nexus cookies are removed from the server."""
    s = shared()
    with s["lock"]:
        _drop(s, token)


def register_run(token, proc) -> None:
    s = shared()
    with s["lock"]:
        s["runs"].setdefault(token, []).append(proc)


def unregister_run(token, proc) -> None:
    s = shared()
    with s["lock"]:
        procs = s["runs"].get(token, [])
        if proc in procs:
            procs.remove(proc)
        if not procs:
            s["runs"].pop(token, None)


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
def read_state_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


def verify_login(user: str, pw: str):
    """Returns (ok, kind, message, nexus_state). Runs the worker in --check-login mode.
    This is the ONLY place the password is used. nexus_state = the Nexus cookies
    from this login; all runs use them instead of the password."""
    with tempfile.TemporaryDirectory(prefix="nexus_chk_") as tmp:
        state_file = Path(tmp) / "state.json"
        env = {**os.environ, "NEXUS_USERNAME": user, "NEXUS_PASSWORD": pw,
               "NEXUS_SCREENSHOT_DIR": str(Path(tmp) / "shots"),
               "NEXUS_STATE_FILE": str(state_file)}
        try:
            proc = subprocess.run(
                [sys.executable, str(WORKER), "--check-login"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except subprocess.TimeoutExpired:
            return False, "system", "Nexus did not answer in time. Please try again.", None
        nexus_state = read_state_file(state_file)      # read before the folder is deleted

    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("RESULT_JSON:"):
            d = json.loads(line[len("RESULT_JSON:"):])
            if d.get("ok"):
                if not nexus_state:
                    log.error("check-login ok, but the Nexus session was not saved")
                    return False, "system", "Could not keep the Nexus session (server problem).", None
                return True, "", "", nexus_state
            if d.get("kind") == "login":
                return False, "login", "Login failed. Check your Nexus ID and password.", None
            if d.get("kind") == "orders":
                log.info("orders page not available: %s", d.get("error"))
                return False, "orders", ("Your login works, but the Orders page could not be "
                                         "opened for this account."), None
            log.error("check-login system error: %s", d.get("error"))
            return False, "system", "Could not check the login (server problem).", None
    log.error("check-login worker gave no result")
    return False, "system", "Could not check the login (server problem).", None


def sign_in_screen() -> None:
    st.title("Nexus Automation Tools")
    st.caption("Sign in with your own Infor Nexus ID and password.")
    with st.form("signin", clear_on_submit=True):     # password box is emptied after submit
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
            valid, kind, msg, nexus_state = verify_login(user, pw)

    if valid:
        clear_failures(user)
        token = create_session(user, nexus_state)
        st.session_state.update(user=user, token=token, set_cookie=token)
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


def summary_excel(rows: list) -> bytes | None:
    """One row per PO with its 'Totals' values. POs that failed go on a 2nd sheet."""
    done = [{"PO": r["po"], **r["totals"]} for r in rows
            if r.get("status") == "done" and r.get("totals")]
    failed = [r for r in rows if r.get("status") != "done"]
    if not done:
        return None

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(done, columns=SUMMARY_COLUMNS).to_excel(
            xw, sheet_name="PO Summary", index=False)
        if failed:
            rows_to_df(failed).to_excel(xw, sheet_name="Not done", index=False)
        for ws in xw.book.worksheets:            # readable column widths
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    return buf.getvalue()


def split_typed_pos(text: str) -> list:
    """'62240228, 62075654 62152463;62168894' -> ['62240228', '62075654', ...]"""
    return [s for s in re.split(r"[\s,;|]+", text) if s]


def po_inputs(key: str) -> list:
    """PO number box (1 to MAX_TYPED_POS numbers) + optional Excel/CSV upload.
    Returns the clean PO list."""
    po_typed = st.text_input(
        f"PO number(s), up to {MAX_TYPED_POS}",
        placeholder="", key=f"{key}_po",
        help="Separate PO numbers with commas or spaces. For more, upload an Excel file.",
    ).strip()
    uploaded = st.file_uploader("Upload an Excel file", type=["xlsx", "csv"],
                                key=f"{key}_file")

    excel_pos, invalid = [], []
    if uploaded is not None:
        try:
            df = load_table(uploaded)
            cols = list(df.columns)
            default = next((i for i, c in enumerate(cols)
                            if str(c).strip().lower() in KNOWN_PO_HEADERS), 0)
            col = st.selectbox("Which column has the PO numbers?", cols, index=default,
                               key=f"{key}_col")
            for v in df[col].tolist():
                s = clean_po(v)
                if not s:
                    continue
                (excel_pos if s.isdigit() else invalid).append(s)
        except Exception as e:
            st.error(f"Could not read the file: {e}")

    typed = split_typed_pos(po_typed)
    if len(typed) > MAX_TYPED_POS:
        st.warning(f"You typed {len(typed)} PO numbers. Only the first {MAX_TYPED_POS} "
                   "are used. For more, upload an Excel file.")
        typed = typed[:MAX_TYPED_POS]
    typed_ok = [s for s in typed if s.isdigit()]
    invalid += [s for s in typed if not s.isdigit()]
    excel_pos = typed_ok + excel_pos                  # typed POs first

    po_list = list(dict.fromkeys(excel_pos))          # remove duplicates, keep order

    if po_list:
        st.info(f"{len(po_list)} PO number(s) ready (maximum {MAX_POS_PER_RUN} per run).")
    if invalid:
        st.warning(f"Ignored {len(invalid)} value(s) that are not digits only: "
                   f"{', '.join(invalid[:5])}{' ...' if len(invalid) > 5 else ''}")
    return po_list


def nexus_session_alive(token) -> bool:
    return bool((get_session(token) or {}).get("nexus_state"))


SESSION_ENDED_MSG = ("Your Nexus session has ended. Download any results below, "
                     "then log out and sign in again.")


def run_batch(po_list: list, user: str, token: str, mode: str = "pdf") -> dict:
    """Run the worker in a private temp folder, show live progress, return rows (+ ZIP).
    mode: "pdf" = PO PDF download, "summary" = PO summary page.
    The worker gets the saved Nexus cookies only, never a password."""
    work = Path(tempfile.mkdtemp(prefix="nexus_run_"))
    out_dir, shots = work / "out", work / "shots"
    po_file = work / "pos.txt"
    po_file.write_text("\n".join(po_list), encoding="utf-8")

    # saved Nexus session
    state_file = work / "state.json"
    nexus_state = (get_session(token) or {}).get("nexus_state")
    if nexus_state:
        state_file.write_text(nexus_state, encoding="utf-8")

    env = {**os.environ, "NEXUS_USERNAME": user, "NEXUS_PASSWORD": "",
           "NEXUS_SCREENSHOT_DIR": str(shots), "NEXUS_STATE_FILE": str(state_file)}
    cmd = [sys.executable, "-u", str(WORKER), "--po-file", str(po_file),
           "--out", str(out_dir), "--headless"]
    if mode == "summary":
        cmd.append("--summary")

    bar = st.progress(0.0, text="Starting browser and logging in...")
    table_box = st.empty()
    rows, log_lines, final, zip_bytes = [], [], None, None
    timed_out = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    register_run(token, proc)

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
                get_session(token)        # long runs count as activity
                bar.progress(d["index"] / d["total"],
                             text=f"{d['index']} of {d['total']} done")
                table_box.dataframe(rows_to_df(rows))
            elif line.startswith("RESULT_JSON:"):
                final = json.loads(line[len("RESULT_JSON:"):])
        proc.wait()
        get_session(token)
        set_nexus_state(token, read_state_file(state_file))   # None -> session ended

        if mode == "pdf" and out_dir.exists() and any(out_dir.glob("*.pdf")):
            archive = shutil.make_archive(str(work / "nexus_pdfs"), "zip", out_dir)
            zip_bytes = Path(archive).read_bytes()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        unregister_run(token, proc)
        shutil.rmtree(work, ignore_errors=True)

    bar.empty()
    table_box.empty()

    counts = {}
    for r in rows:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    log.info("run mode=%s user=%s pos=%d result=%s timed_out=%s",
             mode, user, len(po_list), counts, timed_out.is_set())

    return {"rows": rows, "final": final, "zip": zip_bytes, "timed_out": timed_out.is_set(),
            "total": len(po_list), "log": "\n".join(log_lines)}


def start_button(po_list: list, label: str, state_key: str, mode: str) -> None:
    """Checks the list, takes a browser slot, runs the worker, stores the result."""
    if not st.button(label, type="primary", key=f"{state_key}_start"):
        return
    if not nexus_session_alive(st.session_state["token"]):
        st.error("Your Nexus session has ended. Please log out and sign in again.")
    elif not po_list:
        st.error("Enter a PO number or upload an Excel file first.")
    elif len(po_list) > MAX_POS_PER_RUN:
        st.error(f"Too many POs: {len(po_list)}. Please split the list into runs of "
                 f"{MAX_POS_PER_RUN} or fewer.")
    else:
        with run_slot() as ok:
            if not ok:
                st.warning("The server is busy with other runs. "
                           "Please try again in a few minutes.")
            else:
                st.session_state[state_key] = run_batch(
                    po_list, user, st.session_state["token"], mode)


def batch_problem(batch: dict) -> bool:
    """Shows the timeout / start / login / session messages.
    True if there are no results to show."""
    final, rows = batch["final"] or {}, batch["rows"]
    if batch["timed_out"]:
        st.warning(f"The run was stopped after {MAX_RUN_MINUTES} minutes. "
                   "Results finished so far are shown below. Run the rest again.")
    if not batch["final"] and not rows:
        st.error("The run stopped before it started. Please try again.")
        return True
    if final.get("login_error"):
        if final.get("session_expired"):
            st.error("Your Nexus session has ended. Please log out and sign in again.")
        else:
            st.error("Login to Nexus failed. Please log out and sign in again.")
        return True
    if final.get("session_expired"):          # ended in the middle of the run
        not_done = batch.get("total", len(rows)) - sum(
            1 for r in rows if r.get("status") != "session_expired")
        st.warning(f"Your Nexus session ended during the run. {not_done} PO(s) were not "
                   "processed. Download the results below, then log out, sign in again "
                   "and run those POs.")
    return False


def error_count(counts: dict) -> int:
    return sum(counts.get(k, 0) for k in ("error", "login_failed", "session_expired"))


# page: PO PDF download
def pdf_page() -> None:
    st.title("DPO PDF Downloader")
    po_list = po_inputs("pdf")
    start_button(po_list, "Start download", "batch", "pdf")

    batch = st.session_state.get("batch")
    if not batch or batch_problem(batch):
        return

    df_res = rows_to_df(batch["rows"])
    counts = df_res["Status"].value_counts().to_dict() if len(df_res) else {}
    st.success(
        f"Finished. Downloaded: {counts.get('downloaded', 0)} | "
        f"Not found: {counts.get('not_found', 0)} | "
        f"Errors: {error_count(counts)}"
    )
    st.dataframe(df_res)

    c1, c2 = st.columns(2)
    if batch["zip"]:
        c1.download_button("Download all PDFs (ZIP)", batch["zip"],
                           file_name="nexus_pdfs.zip", mime="application/zip",
                           type="primary")
    c2.download_button("Report (CSV)", df_res.to_csv(index=False).encode("utf-8"),
                       file_name="nexus_download_report.csv", mime="text/csv")


# PO summary page
def summary_page() -> None:
    st.title("Packing Plans")
    po_list = po_inputs("summary")
    start_button(po_list, "Download PO summary", "summary_batch", "summary")

    batch = st.session_state.get("summary_batch")
    if not batch or batch_problem(batch):
        return

    df_res = rows_to_df(batch["rows"])
    counts = df_res["Status"].value_counts().to_dict() if len(df_res) else {}
    st.success(
        f"Finished. Done: {counts.get('done', 0)} | "
        f"Not found: {counts.get('not_found', 0)} | "
        f"Errors: {error_count(counts)}"
    )
    st.dataframe(df_res)

    c1, c2 = st.columns(2)
    if "excel" not in batch:                     # build once, keep for reruns
        batch["excel"] = summary_excel(batch["rows"])
    if batch["excel"]:
        c1.download_button(
            "Download PO summary (Excel)", batch["excel"],
            file_name="nexus_po_summary.xlsx", type="primary",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c2.download_button("Report (CSV)", df_res.to_csv(index=False).encode("utf-8"),
                       file_name="nexus_summary_report.csv", mime="text/csv")


# session restore
if "user" in st.session_state:
    if not get_session(st.session_state.get("token")):     # idle too long
        st.session_state.clear()
        st.session_state["notice"] = "You were signed out after a period of inactivity."
else:
    cookie_token = st.context.cookies.get(COOKIE_NAME)
    sess = get_session(cookie_token)
    if sess:
        st.session_state.update(user=sess["user"], token=cookie_token)

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

with st.sidebar:
    st.write(f"Signed in as **{user}**")
    page_choice = st.radio("Choose a tool", [PAGE_PDF, PAGE_SUMMARY], key="page")
    st.divider()
    if st.button("Log out"):
        drop_session(st.session_state.get("token"))   # removes the saved Nexus cookies
        log.info("logout user=%s", user)
        st.session_state.clear()          # removes all
        st.session_state["clear_cookie"] = True
        st.rerun()

# main page
if not nexus_session_alive(st.session_state["token"]):
    st.warning(SESSION_ENDED_MSG)

if page_choice == PAGE_SUMMARY:
    summary_page()
else:
    pdf_page()
