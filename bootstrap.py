"""
Flow Studio bootstrapper — gatekeeper + first-run setup.

Runs BEFORE the engine (torch/flask/numpy) exists, so this file must import
using the standard library only. It either launches the assembled app or serves
a guided setup UI that builds the environment with `uv` and downloads models.

  bootstrap.exe            → launch app, or run setup if not ready
  bootstrap.exe --selftest → build a throwaway env end-to-end and verify (slow)
"""
import os
import sys
from pathlib import Path

from paths import data_dir


def _program_dir():
    if getattr(sys, "frozen", False):          # PyInstaller exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROGRAM_DIR = _program_dir()
ENV_DIR = PROGRAM_DIR / "env"
VENV_PY = ENV_DIR / "Scripts" / "python.exe"
VENV_PYW = ENV_DIR / "Scripts" / "pythonw.exe"
UV_EXE = PROGRAM_DIR / "uv.exe"
REQUIREMENTS = PROGRAM_DIR / "requirements.txt"
APP_ENTRY = PROGRAM_DIR / "flow_studio.py"

DATA_DIR = data_dir()
MODELS_DIR = DATA_DIR / "models"
MARKER = ENV_DIR / ".setup_complete"
SETUP_LOG = DATA_DIR / "setup.log"


def _fatal(msg):
    """Show an error visible even under --noconsole, then exit non-zero."""
    print(msg, file=sys.stderr)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Flow Studio Setup", 0x10)
    except Exception:
        pass
    sys.exit(1)


import hashlib
import json


def requirements_hash(path=None):
    return hashlib.sha256(Path(path or REQUIREMENTS).read_bytes()).hexdigest()


def read_marker(path=None):
    try:
        return json.loads(Path(path or MARKER).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_marker(models_complete, req_hash=None, path=None):
    data = {"requirements_hash": req_hash or requirements_hash(),
            "models_complete": bool(models_complete)}
    Path(path or MARKER).write_text(json.dumps(data), encoding="utf-8")


def env_ready():
    if not VENV_PY.exists():
        return False
    m = read_marker()
    return (m.get("requirements_hash") == requirements_hash()
            and m.get("models_complete") is True)


import shutil
import socket
import urllib.request


def free_disk_gb(path=None):
    return shutil.disk_usage(str(path or PROGRAM_DIR)).free / 1e9


def reachable(url, timeout=5):
    try:
        urllib.request.urlopen(url, timeout=timeout).read(1)
        return True
    except Exception:
        return False


def precheck(min_gb=4.0):
    problems = []
    if free_disk_gb() < min_gb:
        problems.append(f"Not enough free disk space — about {min_gb:.0f} GB is needed.")
    if not reachable("https://pypi.org/simple/"):
        problems.append("Can't reach PyPI. Setup needs an internet connection this one time.")
    if not reachable("https://huggingface.co"):
        problems.append("Can't reach Hugging Face. Setup needs an internet connection this one time.")
    return problems


SETUP_PORT = 7733


import subprocess

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"

state = {
    "status": "idle",                       # idle | running | done | error
    "step": None,                           # current step name
    "steps": {"engine": "pending", "models": "pending", "cleanup": "pending"},
    "error": "",
    "log_tail": "",
}


def venv_cmd():
    # Pin 3.12: our wheels (torch==2.13.0 etc.) target it. Without --python, uv grabs
    # its newest managed Python (e.g. 3.14), which has no matching wheels.
    return [str(UV_EXE), "venv", "--python", "3.12", str(ENV_DIR)]


def engine_cmd():
    return [str(UV_EXE), "pip", "install", "--python", str(VENV_PY),
            "-r", str(REQUIREMENTS)]


def warm_cmd():
    code = ("import app, flow; "
            "app.get_pipeline('a'); app.get_pipeline('b'); "
            "flow.get_whisper('small'); "
            "print('models ready')")
    return [str(VENV_PY), "-c", code]


def app_env():
    env = dict(os.environ)
    env["HF_HOME"] = str(MODELS_DIR)
    # Keep uv's managed Python in an app-controlled dir, not %APPDATA%\uv — the latter
    # can hold a broken/untrusted managed install that fails traversal (os error 448).
    env["UV_PYTHON_INSTALL_DIR"] = str(DATA_DIR / "uv-python")
    env["UV_PYTHON_PREFERENCE"] = "managed"   # deterministic 3.12, regardless of host Python
    return env


def _default_runner(cmd, env):
    """Run a command, stream combined output to the setup log + state tail."""
    SETUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SETUP_LOG, "a", encoding="utf-8", errors="ignore") as log:
        log.write("\n$ " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(cmd, cwd=str(PROGRAM_DIR), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="ignore")
        for line in proc.stdout:
            log.write(line)
            state["log_tail"] = line.strip()
        return proc.wait()


def run_step(name, cmd, env=None, runner=None):
    runner = runner or _default_runner
    state["status"], state["step"], state["steps"][name] = "running", name, "running"
    try:
        code = runner(cmd, env)
    except Exception as exc:
        code, state["error"] = 1, str(exc)
    if code == 0:
        state["steps"][name] = "done"
        return True
    state["steps"][name] = "error"
    state["status"] = "error"
    if not state["error"]:
        state["error"] = f"Step '{name}' failed. See {SETUP_LOG}."
    return False


def run_setup(runner=None):
    """Engine → models. Marker is written ONLY if both succeed (resumable)."""
    problems = precheck()
    if problems:
        state["status"] = "error"
        state["error"] = " ".join(problems)
        state["steps"]["engine"] = "error"
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    env = app_env()
    # `uv venv` folded into the engine step; idempotent, no-op if env exists.
    if not run_step("engine", venv_cmd(), env=env, runner=runner):
        return
    if run_step("engine", engine_cmd(), env=env, runner=runner) \
            and run_step("models", warm_cmd(), env=env, runner=runner):
        write_marker(True)
        state["status"] = "done"


def ollama_installed():
    return reachable(OLLAMA_URL + "/api/tags", timeout=2) or bool(shutil.which("ollama"))


def ollama_exe():
    found = shutil.which("ollama")
    if found:
        return found
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(cand) if cand.exists() else "ollama"


def ollama_pull_cmd():
    return [ollama_exe(), "pull", "qwen2.5:3b"]


def _verify_signature(path):
    """True only if the file has a Valid Authenticode signature (Windows)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return out == "Valid"
    except Exception:
        return False


def run_cleanup(runner=None):
    """Optional: install Ollama if absent, then pull the cleanup model.
    Never sets the main marker; failure is non-fatal."""
    runner = runner or _default_runner
    state["step"], state["steps"]["cleanup"] = "cleanup", "running"
    try:
        if not ollama_installed():
            dest = DATA_DIR / "OllamaSetup.exe"
            with urllib.request.urlopen(OLLAMA_INSTALLER_URL, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if not _verify_signature(dest):
                state["steps"]["cleanup"] = "error"
                state["error"] = "Downloaded Ollama installer failed signature verification; not running it."
                return
            install_code = runner([str(dest), "/SILENT"], app_env())   # third-party installer may show its own UI
            if install_code != 0:
                state["steps"]["cleanup"] = "error"
                state["error"] = "Ollama installer failed with code %s" % install_code
                return
        code = runner(ollama_pull_cmd(), app_env())
        state["steps"]["cleanup"] = "done" if code == 0 else "error"
    except Exception as exc:
        state["steps"]["cleanup"] = "error"
        state["error"] = "Cleanup setup failed (optional): " + str(exc)


def _launcher_exe():
    """A copy of pythonw.exe named 'Flow Studio.exe' (same Scripts dir, so it still
    finds the venv) so Task Manager shows the process as 'Flow Studio', not
    'pythonw.exe'. Copied once; falls back to pythonw on any error."""
    named = VENV_PYW.with_name("Flow Studio.exe")
    try:
        if not named.exists() or named.stat().st_size != VENV_PYW.stat().st_size:
            import shutil
            shutil.copy2(VENV_PYW, named)
        return named
    except Exception:
        return VENV_PYW


def launch_app():
    subprocess.Popen([str(_launcher_exe()), str(APP_ENTRY)],
                     cwd=str(PROGRAM_DIR), env=app_env())


def should_launch():
    return env_ready()


import threading
import tkinter as tk
from tkinter import ttk


def reset_env():
    if ENV_DIR.exists():
        shutil.rmtree(ENV_DIR, ignore_errors=True)
    for k in state["steps"]:
        state["steps"][k] = "pending"
    state.update(status="idle", step=None, error="", log_tail="")


_STEP_TEXT = {
    "engine": "Engine (PyTorch + libraries)",
    "models": "Voices + speech recognition",
    "cleanup": "Dictation cleanup (optional)",
}
_STEP_MARK = {"pending": "•", "running": "⏳", "done": "✓", "error": "✕"}
_STEP_COLOR = {"pending": "#8A8378", "running": "#E8912D", "done": "#4CA366", "error": "#C0442B"}


def run_setup_ui():
    """Native setup window (tkinter): shows steps + a progress bar and drives
    run_setup / run_cleanup / launch. Worker threads only mutate the shared `state`
    dict; the UI polls it on the main thread via root.after (tkinter is not
    thread-safe, so no widget is touched off the main thread)."""
    root = tk.Tk()
    root.title("Set up Flow Studio")
    root.configure(bg="#F5F1EB")
    root.geometry("540x380")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(PROGRAM_DIR / "flow.ico"))
    except Exception:
        pass

    wrap = tk.Frame(root, bg="#FFFFFF")
    wrap.pack(fill="both", expand=True, padx=18, pady=18)
    tk.Label(wrap, text="Set up Flow Studio", bg="#FFFFFF", fg="#1F1D1A",
             font=("Segoe UI Semibold", 17)).pack(anchor="w", padx=26, pady=(24, 2))
    tk.Label(wrap, text="One-time download (~1.7 GB). Everything is fetched automatically — you just approve.",
             bg="#FFFFFF", fg="#8A8378", font=("Segoe UI", 9), wraplength=470,
             justify="left").pack(anchor="w", padx=26)

    steps = {}
    for key in ("engine", "models", "cleanup"):
        lbl = tk.Label(wrap, text=f"{_STEP_MARK['pending']}  {_STEP_TEXT[key]}",
                       bg="#FFFFFF", fg="#8A8378", font=("Segoe UI", 11), anchor="w")
        lbl.pack(anchor="w", padx=26, pady=2)
        steps[key] = lbl

    bar = ttk.Progressbar(wrap, mode="determinate", maximum=100, length=488)
    bar.pack(padx=26, pady=(16, 4))
    status = tk.Label(wrap, text="", bg="#FFFFFF", fg="#8A8378", font=("Consolas", 8),
                      anchor="w", wraplength=488, justify="left")
    status.pack(fill="x", padx=26)
    errlbl = tk.Label(wrap, text="", bg="#FFFFFF", fg="#C0442B", font=("Segoe UI", 9),
                      anchor="w", wraplength=488, justify="left")
    errlbl.pack(fill="x", padx=26, pady=(4, 0))

    btnrow = tk.Frame(wrap, bg="#FFFFFF")
    btnrow.pack(anchor="w", padx=26, pady=16)

    def _btn(text, dark=True):
        return tk.Button(btnrow, text=text, font=("Segoe UI Semibold", 10), relief="flat",
                         cursor="hand2", padx=18, pady=8, bd=0,
                         bg="#1F1D1A" if dark else "#FFFFFF",
                         fg="#FFFFFF" if dark else "#1F1D1A",
                         activebackground="#000000" if dark else "#F0EADE",
                         activeforeground="#FFFFFF" if dark else "#1F1D1A")

    go, cleanup, launch = _btn("Install"), _btn("Enable cleanup", dark=False), _btn("Open Flow Studio")

    def start_install():
        go.config(state="disabled", text="Installing…")
        threading.Thread(target=run_setup, daemon=True).start()

    def start_cleanup():
        cleanup.config(state="disabled", text="Setting up…")
        threading.Thread(target=run_cleanup, daemon=True).start()

    def do_launch():
        launch.config(state="disabled", text="Opening…")
        threading.Thread(target=launch_app, daemon=True).start()
        root.after(1500, root.destroy)

    go.config(command=start_install)
    cleanup.config(command=start_cleanup)
    launch.config(command=do_launch)
    go.pack(side="left")

    shown = {"done": False}

    def poll():
        for key, lbl in steps.items():
            st = state["steps"].get(key, "pending")
            lbl.config(text=f"{_STEP_MARK[st]}  {_STEP_TEXT[key]}", fg=_STEP_COLOR[st])
        status.config(text=state.get("log_tail", ""))
        errlbl.config(text=state.get("error", ""))
        pct = 0
        for k in ("engine", "models"):
            if state["steps"][k] == "done":
                pct += 50
            elif state["steps"][k] == "running":
                pct += 20
        bar["value"] = pct
        if state["steps"]["engine"] == "done" and state["steps"]["models"] == "done" and not shown["done"]:
            shown["done"] = True
            bar["value"] = 100
            go.pack_forget()
            cleanup.pack(side="left")
            launch.pack(side="left", padx=(8, 0))
        if state["status"] == "error":
            go.config(state="normal", text="Retry")
        root.after(300, poll)

    poll()
    root.mainloop()


def _run_selftest():
    """End-to-end: build a throwaway env, then run the app's own selftest.
    Slow (real uv install + model download). Not part of the fast unit suite."""
    import tempfile
    global ENV_DIR, VENV_PY, VENV_PYW, MARKER
    tmp = Path(tempfile.mkdtemp(prefix="flowstudio_selftest_"))
    ENV_DIR = tmp / "env"
    VENV_PY = ENV_DIR / "Scripts" / "python.exe"
    VENV_PYW = ENV_DIR / "Scripts" / "pythonw.exe"
    MARKER = ENV_DIR / ".setup_complete"
    print("selftest env:", ENV_DIR)
    run_setup()
    if state["status"] == "error":
        print("SETUP FAILED:", state["error"]); return 1
    code = subprocess.call([str(VENV_PY), str(APP_ENTRY), "--selftest"],
                           cwd=str(PROGRAM_DIR), env=app_env())
    print("app selftest exit:", code)
    return 0 if code == 0 else 1


_lock_sock = None


def main():
    if "--check" in sys.argv:
        print("PROGRAM_DIR:", PROGRAM_DIR)
        print("ENV_DIR:", ENV_DIR)
        print("env_ready:", env_ready())
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    if should_launch():
        launch_app()
        return
    # single-instance: hold a loopback port as a lock; if it's taken, setup is
    # already running, so exit rather than build into the same env twice.
    global _lock_sock
    _lock_sock = socket.socket()
    try:
        _lock_sock.bind(("127.0.0.1", SETUP_PORT))
    except OSError:
        return
    run_setup_ui()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
