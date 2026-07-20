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
