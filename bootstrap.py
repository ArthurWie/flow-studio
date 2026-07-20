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

DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowStudio"
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


def free_port(start=7700):
    for p in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:   # nothing listening → free
                return p
    return start


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
    return [str(UV_EXE), "venv", str(ENV_DIR)]


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


def ollama_pull_cmd():
    return ["ollama", "pull", "qwen2.5:3b"]


def run_cleanup(runner=None):
    """Optional: install Ollama if absent, then pull the cleanup model.
    Never sets the main marker; failure is non-fatal."""
    runner = runner or _default_runner
    state["step"], state["steps"]["cleanup"] = "cleanup", "running"
    try:
        if not ollama_installed():
            dest = DATA_DIR / "OllamaSetup.exe"
            urllib.request.urlretrieve(OLLAMA_INSTALLER_URL, str(dest))
            runner([str(dest), "/SILENT"], app_env())   # third-party installer may show its own UI
        code = runner(ollama_pull_cmd(), app_env())
        state["steps"]["cleanup"] = "done" if code == 0 else "error"
    except Exception as exc:
        state["steps"]["cleanup"] = "error"
        state["error"] = "Cleanup setup failed (optional): " + str(exc)
