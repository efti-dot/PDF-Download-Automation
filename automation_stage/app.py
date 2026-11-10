"""
Streamlit app: download Nexus PO pdfs.
  - type ONE PO number, and/or
  - upload an Excel (or CSV) file with many PO numbers
  - choose the folder to save into

Run from the PROJECT ROOT (the folder that contains .env and app/):
    streamlit run app/streamlit_app.py

Needs:  pip install streamlit pandas openpyxl
Runs on YOUR PC. Nexus credentials come from .env, not from this page.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent                      # project root (has .env and .auth)
WORKER = APP_DIR / "nexus_worker.py"

KNOWN_PO_HEADERS = {"po", "po number", "po no", "po id", "po #", "po_number",
                    "order", "order number", "purchase order"}

st.set_page_config(page_title="Nexus PO Downloader", page_icon="📄")
st.title("📄 Nexus PO PDF Downloader")

if "folder" not in st.session_state:
    st.session_state.folder = str(Path.home() / "Documents" / "Nexus_POs")


# ----------------------------- helpers -----------------------------
def browse_folder() -> None:
    """Open the normal Windows folder picker (works because Streamlit runs on your PC)."""
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory() or '')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    chosen = out.stdout.strip()
    if chosen:
        st.session_state.folder = str(Path(chosen))


def open_folder() -> None:
    folder = st.session_state.get("last_folder")
    if folder and os.name == "nt":
        os.startfile(folder)


def clean_po(value) -> str:
    s = str(value).strip()
    if s.lower() == "nan":
        return ""
    if s.endswith(".0"):                  # Excel numbers sometimes become 62397850.0
        s = s[:-2]
    return s


def load_table(uploaded) -> pd.DataFrame:
    if uploaded.name.lower().endswith(".csv"):
        return pd.read_csv(uploaded, dtype=str)
    return pd.read_excel(uploaded, dtype=str)      # dtype=str keeps PO numbers intact


def rows_to_df(rows: list) -> pd.DataFrame:
    return pd.DataFrame([
        {"PO": r.get("po"), "Status": r.get("status"),
         "Details": r.get("path") or r.get("note", "")}
        for r in rows
    ])


def run_batch(po_list: list, folder: Path, headless: bool, skip_existing: bool) -> dict:
    """Start the worker and show live progress."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(po_list))
        po_file = f.name

    cmd = [sys.executable, "-u", str(WORKER), "--po-file", po_file, "--out", str(folder)]
    if headless:
        cmd.append("--headless")
    if skip_existing:
        cmd.append("--skip-existing")

    bar = st.progress(0.0, text="Starting browser and logging in...")
    table_box = st.empty()
    rows, log_lines, final = [], [], None

    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            log_lines.append(line)
            if line.startswith("PROGRESS:"):
                d = json.loads(line[len("PROGRESS:"):])
                rows.append(d["result"])
                bar.progress(d["index"] / d["total"],
                             text=f"{d['index']} of {d['total']} done")
                table_box.dataframe(rows_to_df(rows))
            elif line.startswith("RESULT_JSON:"):
                final = json.loads(line[len("RESULT_JSON:"):])
        proc.wait()
    finally:
        if proc.poll() is None:           # page was refreshed/stopped: stop the worker too
            proc.kill()
        try:
            os.unlink(po_file)
        except OSError:
            pass

    bar.empty()
    table_box.empty()
    return {"rows": rows, "final": final, "log": "\n".join(log_lines)}


# ----------------------------- UI -----------------------------
po_typed = st.text_input("PO number", placeholder="e.g. 62397850").strip()

uploaded = st.file_uploader("Or upload an Excel file with many PO numbers",
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
    st.info(f"{len(po_list)} PO number(s) ready to download.")
if invalid:
    st.warning(f"Ignored {len(invalid)} value(s) that are not digits only: "
               f"{', '.join(invalid[:5])}{' ...' if len(invalid) > 5 else ''}")

c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
c1.text_input("Save folder", key="folder")
c2.button("Browse...", on_click=browse_folder)

headless = st.checkbox("Run browser in the background (headless)", value=False)
skip_existing = st.checkbox("Skip POs already downloaded in this folder", value=True)

if st.button("Download PDF", type="primary"):
    folder = Path(st.session_state.folder).expanduser()
    if not po_list:
        st.error("Enter a PO number or upload an Excel file first.")
    else:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            st.error(f"Cannot use this folder: {e}")
            st.stop()
        st.session_state["batch"] = run_batch(po_list, folder, headless, skip_existing)
        st.session_state["last_folder"] = str(folder)

batch = st.session_state.get("batch")
if batch:
    final = batch["final"]
    rows = batch["rows"]

    if final is None:
        st.error("The worker stopped before finishing. See the log below.")
    elif final.get("login_error"):
        st.error(f"Login failed: {final['login_error']}")
    else:
        df_res = rows_to_df(rows)
        counts = df_res["Status"].value_counts().to_dict() if len(df_res) else {}
        st.success(
            f"Finished. Downloaded: {counts.get('downloaded', 0)} | "
            f"Skipped: {counts.get('skipped', 0)} | "
            f"Not found: {counts.get('not_found', 0)} | "
            f"Errors: {counts.get('error', 0) + counts.get('login_failed', 0)}"
        )
        st.dataframe(df_res)
        st.download_button("Download report (CSV)",
                           df_res.to_csv(index=False).encode("utf-8"),
                           file_name="nexus_download_report.csv", mime="text/csv")
        if os.name == "nt":
            st.button("Open folder", on_click=open_folder)

    with st.expander("Details / log"):
        st.code(batch["log"] or "(no log)")