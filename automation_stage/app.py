import json
import os
import subprocess
import sys
from pathlib import Path
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent                      # project root (has .env and .auth)
WORKER = APP_DIR / "nexus_worker.py"

st.set_page_config(page_title="Nexus PO Downloader", page_icon="📄")
st.title("Nexus PO PDF Downloader")

if "folder" not in st.session_state:
    st.session_state.folder = str(Path.home() / "Documents" / "Nexus_POs")


def browse_folder() -> None:
    """Open a normal Windows folder picker (works because Streamlit runs on your PC)."""
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


def run_worker(po: str, folder: Path, headless: bool) -> dict:
    cmd = [sys.executable, str(WORKER), po, "--out", str(folder)]
    if headless:
        cmd.append("--headless")
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "note": "Timed out after 5 minutes.", "log": ""}

    log = (proc.stdout or "") + (proc.stderr or "")
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("RESULT_JSON:"):
            result = json.loads(line[len("RESULT_JSON:"):])
            result["log"] = log
            return result
    return {"status": "error", "note": "Worker crashed before giving a result.", "log": log}


# UI
po = st.text_input("PO number", placeholder="").strip()

c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
c1.text_input("Save folder", key="folder")
c2.button("Browse...", on_click=browse_folder)

headless = st.checkbox("Run browser in the background (headless)", value=False)

if st.button("Download PDF", type="primary"):
    folder = Path(st.session_state.folder).expanduser()
    if not po.isdigit():
        st.error("Please enter the PO number using digits only.")
    else:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            st.error(f"Cannot use this folder: {e}")
            st.stop()
        with st.spinner(f"Downloading PO {po} from Nexus... (about 10 to 40 seconds)"):
            st.session_state["result"] = run_worker(po, folder, headless)
        st.session_state["last_folder"] = str(folder)

result = st.session_state.get("result")
if result:
    status = result.get("status")
    if status == "downloaded":
        st.success(f"Saved: {result.get('path')}")
        if os.name == "nt":
            st.button("Open folder", on_click=open_folder)
    elif status == "not_found":
        st.warning(f"PO {result.get('po')} was not found in Nexus.")
    elif status == "login_failed":
        st.error(f"Login failed: {result.get('note')}")
    else:
        st.error(result.get("note", "Something went wrong."))

    with st.expander("Details / log"):
        st.code(result.get("log") or "(no log)") #streamlit run automation_stage/app.py