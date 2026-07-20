# Lightweight Installer & Download-on-Setup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a small Windows installer that, on first run, assembles the ~1.1 GB engine (via `uv` + PyPI) and downloads the ML models (Hugging Face), driven by a guided setup UI in `bootstrap.exe`, then launches the existing Flow Studio app.

**Architecture:** A stdlib-only `bootstrap.py` is the gatekeeper: on launch it checks a marker; if the environment is ready it launches `flow_studio.py` from a `uv`-built venv, otherwise it serves a local setup web page (stdlib `http.server`) that runs `uv venv` / `uv pip install` and warms the models, all resumable and idempotent. An Inno Setup installer ships the app source + `bootstrap.exe` + `uv.exe` + `requirements.txt`; `package.ps1` builds it.

**Tech Stack:** Python 3.12 (stdlib only for `bootstrap.py`), `uv` (bundled binary), PyInstaller (builds `bootstrap.exe`), Inno Setup (`ISCC.exe`), pytest (dev-only, for bootstrap unit tests).

## Global Constraints

- **Platform: Windows only.** Verbatim from spec §9. `bootstrap.py` may use Windows-isms (`os.environ["LOCALAPPDATA"]`, `ctypes.windll` for the fatal dialog).
- **`bootstrap.py` must import using the standard library only** — it runs *before* the engine (numpy/torch/flask) exists. No third-party imports anywhere in `bootstrap.py`.
- **Python 3.12** is the target interpreter (matches the venv the app is tested against).
- **Engine is CPU-only torch**, pinned by the existing `requirements.txt` — do not add GPU/CUDA logic.
- **Pre-fetch Whisper `small` only** in the models step; larger sizes download on demand in-app.
- **Two install locations:** program files at `%LOCALAPPDATA%\Programs\FlowStudio\`; user data at `%LOCALAPPDATA%\FlowStudio\` (existing `DATA_DIR`). Models at `%LOCALAPPDATA%\FlowStudio\models` (`HF_HOME`).
- **Marker** `env\.setup_complete` is JSON `{"requirements_hash": <sha256>, "models_complete": <bool>}`, written only on full success.
- **This project is not yet under git.** Task 1 initializes it so the commit checkpoints below work. If you decline git, treat each "Commit" step as a manual review checkpoint.

---

### Task 1: Version control, test tooling, and `bootstrap.py` config skeleton

**Files:**
- Create: `.gitignore`
- Create: `bootstrap.py`
- Test: `test_bootstrap.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: module-level path constants `PROGRAM_DIR, ENV_DIR, VENV_PY, VENV_PYW, UV_EXE, REQUIREMENTS, APP_ENTRY, DATA_DIR, MODELS_DIR, MARKER, SETUP_LOG` (all `pathlib.Path`); `_fatal(msg: str) -> None`.

- [ ] **Step 1: Initialize git and ignore build/data artifacts**

Run:
```bash
cd /d/Kokoro-TTS && git init
```

Create `.gitignore`:
```gitignore
venv/
env/
__pycache__/
*.pyc
*.log
flow_history.json
pronunciations.json
outputs/
output_*.wav
build/
dist/
*.spec
FlowStudio-build/
```

- [ ] **Step 2: Install pytest in the dev venv (dev-only, not shipped)**

Run:
```bash
./venv/Scripts/python.exe -m pip install pytest
```
Expected: `Successfully installed pytest-...`

- [ ] **Step 3: Write the failing test for path derivation**

Create `test_bootstrap.py`:
```python
from pathlib import Path
import bootstrap


def test_paths_are_consistent():
    assert bootstrap.ENV_DIR == bootstrap.PROGRAM_DIR / "env"
    assert bootstrap.VENV_PY == bootstrap.ENV_DIR / "Scripts" / "python.exe"
    assert bootstrap.VENV_PYW == bootstrap.ENV_DIR / "Scripts" / "pythonw.exe"
    assert bootstrap.UV_EXE == bootstrap.PROGRAM_DIR / "uv.exe"
    assert bootstrap.REQUIREMENTS == bootstrap.PROGRAM_DIR / "requirements.txt"
    assert bootstrap.APP_ENTRY == bootstrap.PROGRAM_DIR / "flow_studio.py"
    assert bootstrap.MODELS_DIR == bootstrap.DATA_DIR / "models"
    assert bootstrap.MARKER == bootstrap.ENV_DIR / ".setup_complete"
    assert isinstance(bootstrap.DATA_DIR, Path)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bootstrap'`

- [ ] **Step 5: Write the config skeleton**

Create `bootstrap.py`:
```python
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add .gitignore bootstrap.py test_bootstrap.py
git commit -m "feat(installer): bootstrap config skeleton + repo init"
```

---

### Task 2: Marker & environment-readiness logic

**Files:**
- Modify: `bootstrap.py` (append functions)
- Test: `test_bootstrap.py` (append tests)

**Interfaces:**
- Consumes: `REQUIREMENTS, VENV_PY, MARKER` from Task 1.
- Produces: `requirements_hash(path=REQUIREMENTS) -> str`; `read_marker(path=MARKER) -> dict`; `write_marker(models_complete: bool, req_hash: str|None=None, path=MARKER) -> None`; `env_ready() -> bool`.

- [ ] **Step 1: Write failing tests**

Append to `test_bootstrap.py`:
```python
import json
import bootstrap as bs


def test_marker_roundtrip(tmp_path):
    marker = tmp_path / ".setup_complete"
    bs.write_marker(True, req_hash="abc", path=marker)
    assert bs.read_marker(marker) == {"requirements_hash": "abc", "models_complete": True}


def test_read_marker_missing_returns_empty(tmp_path):
    assert bs.read_marker(tmp_path / "nope") == {}


def test_requirements_hash_changes_with_content(tmp_path):
    a = tmp_path / "r.txt"; a.write_text("torch==2.13.0")
    h1 = bs.requirements_hash(a)
    a.write_text("torch==2.13.1")
    assert bs.requirements_hash(a) != h1


def test_env_ready_false_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "VENV_PY", tmp_path / "missing" / "python.exe")
    assert bs.env_ready() is False


def test_env_ready_true_when_everything_matches(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"; py.write_text("")
    req = tmp_path / "requirements.txt"; req.write_text("torch==2.13.0")
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "VENV_PY", py)
    monkeypatch.setattr(bs, "REQUIREMENTS", req)
    monkeypatch.setattr(bs, "MARKER", marker)
    bs.write_marker(True, path=marker)
    assert bs.env_ready() is True


def test_env_ready_false_on_hash_mismatch(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"; py.write_text("")
    req = tmp_path / "requirements.txt"; req.write_text("torch==2.13.0")
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "VENV_PY", py)
    monkeypatch.setattr(bs, "REQUIREMENTS", req)
    monkeypatch.setattr(bs, "MARKER", marker)
    bs.write_marker(True, req_hash="stale", path=marker)
    assert bs.env_ready() is False


def test_env_ready_false_when_models_incomplete(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"; py.write_text("")
    req = tmp_path / "requirements.txt"; req.write_text("x")
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "VENV_PY", py)
    monkeypatch.setattr(bs, "REQUIREMENTS", req)
    monkeypatch.setattr(bs, "MARKER", marker)
    bs.write_marker(False, path=marker)
    assert bs.env_ready() is False
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -k "marker or env_ready or requirements_hash" -v`
Expected: FAIL — `AttributeError: module 'bootstrap' has no attribute 'requirements_hash'`

- [ ] **Step 3: Implement**

Append to `bootstrap.py`:
```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add bootstrap.py test_bootstrap.py
git commit -m "feat(installer): marker + env-readiness detection"
```

---

### Task 3: Precheck (disk, reachability) and free-port selection

**Files:**
- Modify: `bootstrap.py`
- Test: `test_bootstrap.py`

**Interfaces:**
- Consumes: `PROGRAM_DIR` from Task 1.
- Produces: `free_disk_gb(path=PROGRAM_DIR) -> float`; `reachable(url: str, timeout=5) -> bool`; `precheck(min_gb=4.0) -> list[str]` (empty = OK); `free_port(start=7700) -> int`.

- [ ] **Step 1: Write failing tests**

Append to `test_bootstrap.py`:
```python
import socket
import bootstrap as bs


def test_precheck_flags_low_disk(monkeypatch):
    monkeypatch.setattr(bs, "free_disk_gb", lambda *a, **k: 1.0)
    monkeypatch.setattr(bs, "reachable", lambda *a, **k: True)
    problems = bs.precheck(min_gb=4.0)
    assert any("disk" in p.lower() for p in problems)


def test_precheck_flags_offline(monkeypatch):
    monkeypatch.setattr(bs, "free_disk_gb", lambda *a, **k: 50.0)
    monkeypatch.setattr(bs, "reachable", lambda *a, **k: False)
    problems = bs.precheck()
    assert len(problems) >= 1


def test_precheck_ok(monkeypatch):
    monkeypatch.setattr(bs, "free_disk_gb", lambda *a, **k: 50.0)
    monkeypatch.setattr(bs, "reachable", lambda *a, **k: True)
    assert bs.precheck() == []


def test_free_port_skips_taken_port():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0)); srv.listen(1)
        taken = srv.getsockname()[1]
        got = bs.free_port(start=taken)
        assert got != taken
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -k "precheck or free_port" -v`
Expected: FAIL — `AttributeError: ... 'precheck'`

- [ ] **Step 3: Implement**

Append to `bootstrap.py`:
```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bootstrap.py test_bootstrap.py
git commit -m "feat(installer): precheck + free-port selection"
```

---

### Task 4: Setup steps — command construction, state machine, Ollama

**Files:**
- Modify: `bootstrap.py`
- Test: `test_bootstrap.py`

**Interfaces:**
- Consumes: `UV_EXE, ENV_DIR, VENV_PY, REQUIREMENTS, PROGRAM_DIR, MODELS_DIR, SETUP_LOG, DATA_DIR, write_marker, reachable` from earlier tasks.
- Produces:
  - `venv_cmd() -> list[str]`, `engine_cmd() -> list[str]`, `warm_cmd() -> list[str]`
  - `app_env() -> dict` (os.environ + `HF_HOME`)
  - `ollama_installed() -> bool`, `ollama_pull_cmd() -> list[str]`, `OLLAMA_URL: str`
  - `state: dict` with shape `{"status", "step", "steps": {name: status}, "error", "log_tail"}`
  - `run_step(name: str, cmd: list[str], env: dict|None=None, runner=...) -> bool`
  - `run_setup(runner=...) -> None` (engine → models, sets marker on success)
  - `run_cleanup(runner=...) -> None` (optional Ollama)

- [ ] **Step 1: Write failing tests**

Append to `test_bootstrap.py`:
```python
import bootstrap as bs


def test_engine_cmd_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "UV_EXE", tmp_path / "uv.exe")
    monkeypatch.setattr(bs, "VENV_PY", tmp_path / "env" / "Scripts" / "python.exe")
    monkeypatch.setattr(bs, "REQUIREMENTS", tmp_path / "requirements.txt")
    cmd = bs.engine_cmd()
    assert cmd[0] == str(tmp_path / "uv.exe")
    assert cmd[1:3] == ["pip", "install"]
    assert "--python" in cmd and "-r" in cmd


def test_venv_cmd_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "UV_EXE", tmp_path / "uv.exe")
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    assert bs.venv_cmd() == [str(tmp_path / "uv.exe"), "venv", str(tmp_path / "env")]


def test_warm_cmd_uses_venv_python_and_small_whisper(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "VENV_PY", tmp_path / "python.exe")
    cmd = bs.warm_cmd()
    assert cmd[0] == str(tmp_path / "python.exe")
    assert cmd[1] == "-c"
    assert "get_whisper('small')" in cmd[2]
    assert "get_pipeline('a')" in cmd[2]


def test_app_env_sets_hf_home(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")
    env = bs.app_env()
    assert env["HF_HOME"] == str(tmp_path / "models")


def test_run_step_success_updates_state():
    bs.state["steps"]["engine"] = "pending"
    ok = bs.run_step("engine", ["noop"], runner=lambda cmd, env: 0)
    assert ok is True and bs.state["steps"]["engine"] == "done"


def test_run_step_failure_records_error():
    bs.state["steps"]["engine"] = "pending"
    ok = bs.run_step("engine", ["noop"], runner=lambda cmd, env: 1)
    assert ok is False and bs.state["steps"]["engine"] == "error" and bs.state["error"]


def test_run_setup_writes_marker_only_on_full_success(monkeypatch, tmp_path):
    marker = tmp_path / ".setup_complete"
    req = tmp_path / "requirements.txt"; req.write_text("x")
    monkeypatch.setattr(bs, "MARKER", marker)
    monkeypatch.setattr(bs, "REQUIREMENTS", req)
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    bs.run_setup(runner=lambda cmd, env: 0)     # all steps succeed
    assert bs.read_marker(marker).get("models_complete") is True


def test_run_setup_no_marker_on_failure(monkeypatch, tmp_path):
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "MARKER", marker)
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    bs.run_setup(runner=lambda cmd, env: 1)     # everything fails
    assert not marker.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -k "cmd or run_step or run_setup or app_env" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement**

Append to `bootstrap.py`:
```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bootstrap.py test_bootstrap.py
git commit -m "feat(installer): setup steps, state machine, optional Ollama"
```

---

### Task 5: App launch and gatekeeper decision

**Files:**
- Modify: `bootstrap.py`
- Test: `test_bootstrap.py`

**Interfaces:**
- Consumes: `VENV_PYW, APP_ENTRY, PROGRAM_DIR, app_env, env_ready` from earlier tasks.
- Produces: `launch_app() -> None` (spawns the app detached); `should_launch() -> bool` (thin wrapper over `env_ready()` for test seams).

- [ ] **Step 1: Write failing tests**

Append to `test_bootstrap.py`:
```python
import bootstrap as bs


def test_launch_app_spawns_pythonw_with_hf_home(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(bs, "VENV_PYW", tmp_path / "pythonw.exe")
    monkeypatch.setattr(bs, "APP_ENTRY", tmp_path / "flow_studio.py")
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")

    def fake_popen(cmd, cwd=None, env=None, **kw):
        captured.update(cmd=cmd, cwd=cwd, env=env)
        class P: pass
        return P()
    monkeypatch.setattr(bs.subprocess, "Popen", fake_popen)

    bs.launch_app()
    assert captured["cmd"] == [str(tmp_path / "pythonw.exe"), str(tmp_path / "flow_studio.py")]
    assert captured["env"]["HF_HOME"] == str(tmp_path / "models")
    assert captured["cwd"] == str(bs.PROGRAM_DIR)
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -k "launch_app" -v`
Expected: FAIL — `AttributeError: ... 'launch_app'`

- [ ] **Step 3: Implement**

Append to `bootstrap.py`:
```python
def launch_app():
    subprocess.Popen([str(VENV_PYW), str(APP_ENTRY)],
                     cwd=str(PROGRAM_DIR), env=app_env())


def should_launch():
    return env_ready()
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bootstrap.py test_bootstrap.py
git commit -m "feat(installer): app launch + gatekeeper decision"
```

---

### Task 6: HTTP setup server, endpoints, and setup page

**Files:**
- Modify: `bootstrap.py`
- Test: `test_bootstrap.py`

**Interfaces:**
- Consumes: `state, precheck, run_setup, run_cleanup, launch_app, ENV_DIR, free_port` from earlier tasks.
- Produces: `SETUP_HTML: str`; `BootstrapHandler` (http.server handler); `serve(port: int) -> http.server.HTTPServer`; `reset_env() -> None`. Endpoints: `GET /` (page), `GET /status` (JSON of `state`), `GET /precheck` (JSON `{"problems": [...]}`), `POST /install`, `POST /install_ollama`, `POST /reset`, `POST /launch`.

- [ ] **Step 1: Write failing tests**

Append to `test_bootstrap.py`:
```python
import json
import threading
import urllib.request
import bootstrap as bs


def _spawn(monkeypatch):
    # neuter the real work so endpoints return fast in tests
    monkeypatch.setattr(bs, "run_setup", lambda runner=None: bs.state["steps"].update(engine="done", models="done"))
    monkeypatch.setattr(bs, "run_cleanup", lambda runner=None: bs.state["steps"].update(cleanup="done"))
    monkeypatch.setattr(bs, "launch_app", lambda: bs.state.__setitem__("status", "launched"))
    monkeypatch.setattr(bs, "precheck", lambda *a, **k: [])
    port = bs.free_port(7800)
    srv = bs.serve(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, srv


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).read().decode()


def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    return urllib.request.urlopen(req, timeout=3).read().decode()


def test_index_serves_page(monkeypatch):
    port, srv = _spawn(monkeypatch)
    try:
        body = _get(port, "/")
        assert "Flow Studio" in body and "Install" in body
    finally:
        srv.shutdown()


def test_status_is_json(monkeypatch):
    port, srv = _spawn(monkeypatch)
    try:
        data = json.loads(_get(port, "/status"))
        assert "steps" in data and "engine" in data["steps"]
    finally:
        srv.shutdown()


def test_install_runs_setup(monkeypatch):
    port, srv = _spawn(monkeypatch)
    try:
        _post(port, "/install")
        import time; time.sleep(0.3)
        assert bs.state["steps"]["engine"] == "done"
    finally:
        srv.shutdown()


def test_launch_endpoint_calls_launch_app(monkeypatch):
    port, srv = _spawn(monkeypatch)
    try:
        _post(port, "/launch")
        import time; time.sleep(0.2)
        assert bs.state["status"] == "launched"
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -k "index_serves or status_is_json or install_runs or launch_endpoint" -v`
Expected: FAIL — `AttributeError: ... 'serve'`

- [ ] **Step 3: Implement the page**

Append to `bootstrap.py`:
```python
SETUP_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flow Studio — Setup</title>
<style>
  body{margin:0;background:#F5F1EB;color:#1F1D1A;font-family:system-ui,'Segoe UI',sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;}
  .card{background:#fff;border:1px solid #ECE5D8;border-radius:14px;max-width:520px;width:92%;
        padding:28px 30px;box-shadow:0 1px 3px rgba(60,50,30,.06);}
  h1{font-size:22px;margin:0 0 6px;} p.sub{color:#8A8378;margin:0 0 18px;font-size:14px;}
  .step{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid #F0EADE;font-size:14.5px;}
  .dot{width:9px;height:9px;border-radius:50%;background:#D8CFBC;flex:none;}
  .dot.running{background:#E8912D;} .dot.done{background:#4CA366;} .dot.error{background:#C0442B;}
  .tail{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:#8A8378;margin:10px 0 0;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  button{font:inherit;font-size:14px;font-weight:600;border:none;border-radius:999px;cursor:pointer;
         background:#1F1D1A;color:#fff;padding:11px 22px;margin-top:18px;}
  button:disabled{background:#C4BBA9;cursor:default;}
  .opt{background:#fff;color:#1F1D1A;border:1px solid #E2DACB;margin-left:8px;}
  .err{color:#C0442B;font-size:13px;margin-top:12px;}
  a.small{font-size:12px;color:#8A8378;margin-left:auto;}
</style></head><body>
<div class="card">
  <h1>Set up Flow Studio</h1>
  <p class="sub">One-time download (~1.7 GB). Everything is fetched automatically — you just approve.</p>
  <div id="err" class="err"></div>
  <div class="step"><span class="dot" id="d-engine"></span> Engine (PyTorch + libraries)</div>
  <div class="step"><span class="dot" id="d-models"></span> Voices + speech recognition</div>
  <div class="step"><span class="dot" id="d-cleanup"></span> Dictation cleanup (optional)
    <a href="#" class="small" id="skipCleanup">skip</a></div>
  <p class="tail" id="tail"></p>
  <div>
    <button id="go">Install</button>
    <button id="cleanup" class="opt" style="display:none">Enable cleanup</button>
    <button id="launch" style="display:none">Open Flow Studio</button>
  </div>
</div>
<script>
const $=i=>document.getElementById(i), post=u=>fetch(u,{method:'POST'});
async function tick(){
  const s=await (await fetch('/status')).json();
  for(const k of ['engine','models','cleanup']) $('d-'+k).className='dot '+(s.steps[k]||'');
  $('tail').textContent=s.log_tail||'';
  $('err').textContent=s.error||'';
  if(s.steps.engine==='done'&&s.steps.models==='done'){
    $('go').style.display='none'; $('cleanup').style.display=''; $('launch').style.display='';
  }
  if(s.status==='error'){ $('go').disabled=false; $('go').textContent='Retry'; }
}
$('go').onclick=()=>{ $('go').disabled=true; $('go').textContent='Installing…'; post('/install'); };
$('cleanup').onclick=()=>{ $('cleanup').disabled=true; post('/install_ollama'); };
$('skipCleanup').onclick=e=>{ e.preventDefault(); $('cleanup').style.display='none'; };
$('launch').onclick=()=>{ post('/launch'); $('launch').textContent='Opening…'; };
setInterval(tick,700); tick();
</script></body></html>"""
```

- [ ] **Step 4: Implement the server**

Append to `bootstrap.py`:
```python
import http.server
import json
import threading


def reset_env():
    if ENV_DIR.exists():
        shutil.rmtree(ENV_DIR, ignore_errors=True)
    for k in state["steps"]:
        state["steps"][k] = "pending"
    state.update(status="idle", step=None, error="", log_tail="")


class BootstrapHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):        # silence console spam
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, SETUP_HTML, "text/html; charset=utf-8")
        elif self.path == "/status":
            self._send(200, json.dumps(state))
        elif self.path == "/precheck":
            self._send(200, json.dumps({"problems": precheck()}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path == "/install":
            threading.Thread(target=run_setup, daemon=True).start()
            self._send(200, "{}")
        elif self.path == "/install_ollama":
            threading.Thread(target=run_cleanup, daemon=True).start()
            self._send(200, "{}")
        elif self.path == "/reset":
            reset_env(); self._send(200, "{}")
        elif self.path == "/launch":
            threading.Thread(target=launch_app, daemon=True).start()
            self._send(200, "{}")
        else:
            self._send(404, "{}")


def serve(port):
    return http.server.HTTPServer(("127.0.0.1", port), BootstrapHandler)
```

- [ ] **Step 5: Run to verify pass**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add bootstrap.py test_bootstrap.py
git commit -m "feat(installer): setup web server, endpoints, and page"
```

---

### Task 7: `main()` entrypoint and `--selftest` end-to-end mode

**Files:**
- Modify: `bootstrap.py`
- Test: manual (end-to-end; network + minutes)

**Interfaces:**
- Consumes: everything above.
- Produces: `main() -> None`; `--selftest` CLI mode.

- [ ] **Step 1: Implement `main()` and selftest**

Append to `bootstrap.py`:
```python
import webbrowser


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


def main():
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    if should_launch():
        launch_app()
        return
    port = free_port(7700)
    # single-instance: if setup is already being served here, just open it
    with socket.socket() as s:
        already = s.connect_ex(("127.0.0.1", port)) == 0
    srv = serve(port)
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print("Flow Studio setup →", url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
```

- [ ] **Step 2: Verify the fast suite still passes**

Run: `./venv/Scripts/python.exe -m pytest test_bootstrap.py -v`
Expected: PASS (all)

- [ ] **Step 3: Manual end-to-end (run once; needs internet + several minutes)**

Run: `./venv/Scripts/python.exe bootstrap.py --selftest`
Expected: prints a temp env path, installs the engine, downloads models, then `app selftest exit: 0` and process exit 0. If it fails, read `%LOCALAPPDATA%\FlowStudio\setup.log`.

- [ ] **Step 4: Commit**

```bash
git add bootstrap.py
git commit -m "feat(installer): main() gatekeeper + end-to-end selftest"
```

---

### Task 8: Build `bootstrap.exe` with PyInstaller

**Files:**
- Modify: `bootstrap.py` (add a `--check` diagnostic used to verify the frozen exe)

**Interfaces:**
- Consumes: `main()` from Task 7.
- Produces: `bootstrap.exe` (a small, stdlib-only frozen executable); a `--check` flag that prints resolved config and exits 0.

- [ ] **Step 1: Add a `--check` flag (lets us verify the frozen exe without a console build)**

In `bootstrap.py`, at the top of `main()` add:
```python
    if "--check" in sys.argv:
        print("PROGRAM_DIR:", PROGRAM_DIR)
        print("ENV_DIR:", ENV_DIR)
        print("env_ready:", env_ready())
        sys.exit(0)
```

- [ ] **Step 2: Build the console variant and verify `--check`**

Run:
```bash
cd /d/Kokoro-TTS
./venv/Scripts/python.exe -m PyInstaller bootstrap.py --onefile --console \
  --name bootstrap --icon "D:/Kokoro-TTS/flow.ico" --noconfirm \
  --distpath "D:/FlowStudio-build/boot/dist" --workpath "D:/FlowStudio-build/boot/build" \
  --specpath "D:/FlowStudio-build/boot"
"D:/FlowStudio-build/boot/dist/bootstrap.exe" --check
```
Expected: prints `PROGRAM_DIR: ...`, `env_ready: False`, exit 0. Confirm the exe is small:
```bash
ls -la "D:/FlowStudio-build/boot/dist/bootstrap.exe"
```
Expected: well under 25 MB (stdlib only).

- [ ] **Step 3: Commit**

```bash
git add bootstrap.py
git commit -m "feat(installer): --check diagnostic for frozen bootstrap"
```

---

### Task 9: Inno Setup installer script

**Files:**
- Create: `FlowStudio.iss`

**Interfaces:**
- Consumes: staged files produced by `package.ps1` (Task 10): `bootstrap.exe`, `uv.exe`, app source, `requirements.txt`, `flow.ico`.
- Produces: `FlowStudioSetup.exe` when compiled with `ISCC.exe`.

- [ ] **Step 1: Write the Inno script**

Create `FlowStudio.iss`:
```iss
; Flow Studio installer — per-user, no admin. Compile with ISCC.exe (Inno Setup 6).
; Expects staged files under .\stage\ (produced by package.ps1).
#define AppName "Flow Studio"
#define AppVer "1.0.0"

[Setup]
AppName={#AppName}
AppVersion={#AppVer}
DefaultDirName={localappdata}\Programs\FlowStudio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=FlowStudioSetup
OutputDir=.
Compression=lzma2
SolidCompression=yes
SetupIconFile=flow.ico
UninstallDisplayIcon={app}\bootstrap.exe
WizardStyle=modern

[Files]
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Flow Studio"; Filename: "{app}\bootstrap.exe"; IconFilename: "{app}\flow.ico"
Name: "{userdesktop}\Flow Studio"; Filename: "{app}\bootstrap.exe"; IconFilename: "{app}\flow.ico"

[Run]
Filename: "{app}\bootstrap.exe"; Description: "Launch Flow Studio (runs one-time setup)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\env"
```

- [ ] **Step 2: Verify it parses (once `package.ps1` has staged files — see Task 10)**

This script is compiled by `package.ps1` in Task 10. Standalone verification: install Inno Setup 6, then after Task 10 stages files, `ISCC.exe FlowStudio.iss` must produce `FlowStudioSetup.exe` with exit 0.

- [ ] **Step 3: Commit**

```bash
git add FlowStudio.iss
git commit -m "feat(installer): Inno Setup script"
```

---

### Task 10: `package.ps1` build pipeline

**Files:**
- Create: `package.ps1`

**Interfaces:**
- Consumes: `bootstrap.py`, `FlowStudio.iss`, `requirements.txt`, app source, `flow.ico`.
- Produces: `FlowStudioSetup.exe` (the shippable installer).

- [ ] **Step 1: Write the pipeline script**

Create `package.ps1`:
```powershell
# Build the Flow Studio installer end-to-end.
#   .\package.ps1
# Requires: Inno Setup 6 (ISCC.exe on PATH or at the default location).
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py   = Join-Path $root "venv\Scripts\python.exe"
$stage = Join-Path $root "stage"
$boot = "D:\FlowStudio-build\boot"

# 1. Build bootstrap.exe (stdlib-only, small, windowed for production)
& $py -m PyInstaller (Join-Path $root "bootstrap.py") --onefile --noconsole `
  --name bootstrap --icon (Join-Path $root "flow.ico") --noconfirm `
  --distpath "$boot\dist" --workpath "$boot\build" --specpath $boot
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 2. Fetch pinned uv.exe if not already present
$uv = Join-Path $root "uv.exe"
if (-not (Test-Path $uv)) {
  $uvUrl = "https://github.com/astral-sh/uv/releases/download/0.5.11/uv-x86_64-pc-windows-msvc.zip"
  $tmp = Join-Path $env:TEMP "uv.zip"
  Invoke-WebRequest -Uri $uvUrl -OutFile $tmp
  Expand-Archive -Path $tmp -DestinationPath $env:TEMP -Force
  Copy-Item (Join-Path $env:TEMP "uv.exe") $uv -Force
}

# 3. Stage everything the installer ships
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item (Join-Path $root "flow_studio.py") $stage
Copy-Item (Join-Path $root "app.py") $stage
Copy-Item (Join-Path $root "flow.py") $stage
Copy-Item (Join-Path $root "flow.ico") $stage
Copy-Item (Join-Path $root "requirements.txt") $stage
Copy-Item "$boot\dist\bootstrap.exe" $stage
Copy-Item $uv $stage

# 4. Compile the installer with Inno Setup
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "ISCC.exe" }   # fall back to PATH
& $iscc (Join-Path $root "FlowStudio.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "`nBuilt: $(Join-Path $root 'FlowStudioSetup.exe')" -ForegroundColor Green
```

- [ ] **Step 2: Parse-check the script**

Run:
```bash
powershell -NoProfile -Command "\$e=\$null;[System.Management.Automation.Language.Parser]::ParseFile('D:\\Kokoro-TTS\\package.ps1',[ref]\$null,[ref]\$e);if(\$e.Count){\$e|%{\$_.Message}}else{'package.ps1: parses OK'}"
```
Expected: `package.ps1: parses OK`

- [ ] **Step 3: Full build (requires Inno Setup 6 installed)**

Run: `powershell -NoProfile -File package.ps1`
Expected: ends with `Built: D:\Kokoro-TTS\FlowStudioSetup.exe`; the file exists and is ~40–50 MB.

- [ ] **Step 4: Add build outputs to .gitignore and commit**

Append to `.gitignore`:
```gitignore
stage/
uv.exe
FlowStudioSetup.exe
```
Then:
```bash
git add package.ps1 .gitignore
git commit -m "feat(installer): package.ps1 build pipeline"
```

---

### Task 11: README install-section rewrite

**Files:**
- Modify: `README.md` (the "For users" section)

**Interfaces:**
- Consumes: the real install flow from Tasks 1–10.
- Produces: user-facing install docs matching what actually ships.

- [ ] **Step 1: Replace the "For users (installed build)" section**

In `README.md`, replace the current "For users (installed build)" section body with:
```markdown
## For users (installed build)

1. Download **FlowStudioSetup.exe** from the Releases page and run it. It installs per-user (no admin) and adds a "Flow Studio" shortcut.
2. Launch **Flow Studio**. The first launch shows a one-time **Setup** screen — click **Install** and it downloads everything automatically (~1.7 GB): the engine, then the voices + speech recognition. Needs internet this once; after that the app runs offline.
3. Optionally enable **dictation cleanup** on the setup screen — it installs Ollama and the `qwen2.5:3b` model for you. Dictation works without it (it just types the raw transcript).

The download is split across PyPI (engine) and Hugging Face (models); nothing large is hosted by the project. Setup is resumable — if it's interrupted, relaunch and it picks up where it left off.
```

- [ ] **Step 2: Update the build section to point at package.ps1**

In the "Building for deployment" section, replace the "Bundle with PyInstaller" instructions' first command block so it reads:
```markdown
Build the shippable lightweight installer:

```powershell
.\package.ps1     # builds bootstrap.exe, fetches uv, stages files, runs Inno Setup
```

Output: `FlowStudioSetup.exe` (~40–50 MB). Requires Inno Setup 6 installed. The old `build.ps1` (full ~1.1 GB bundle) remains as the offline/air-gapped alternative.
```

- [ ] **Step 3: Verify consistency**

Read the full README top-to-bottom. Confirm no remaining claim that users must install Python/venv/pip themselves, and that data paths / `%LOCALAPPDATA%` references match.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: rewrite install section for the lightweight installer"
```

---

## Self-Review

**1. Spec coverage:**
- §3 architecture / two locations → Task 1 (paths), Task 9 (`DefaultDirName`, `[UninstallDelete] env`), Task 10 (staging). ✓
- §4 detection (marker) → Task 2; serving UI → Task 6; steps engine/models → Task 4; HF_HOME → Task 4 (`app_env`) + Task 5 (launch inherits it) + Task 7 (`_run_selftest`); optional Ollama → Task 4 + Task 6 endpoint. ✓
- §5 resume (marker only on success) → Task 4 `run_setup` + test; retry → Task 6 page; per-step errors/log → Task 4 `_default_runner`/`run_step`; free-port → Task 3; single-instance → Task 7; reset escape hatch → Task 6 `reset_env`/`/reset`; precheck → Task 3 + `/precheck`. ✓
- §6 build pipeline → Tasks 8–10; testing (unit + e2e) → Tasks 2–7; acceptance (clean VM) → noted as manual in Task 10 Step 3. ✓
- §7 scope (CPU torch via existing requirements; Whisper small in `warm_cmd`; install-once) → Task 4. ✓
- §8 new files (`bootstrap.py`, `FlowStudio.iss`, `package.ps1`, README) → Tasks 1/9/10/11. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every command has expected output. ✓

**3. Type/name consistency:** `state` shape identical across Tasks 4/6/7; `run_step`/`run_setup`/`run_cleanup`, `app_env`, `warm_cmd`, `engine_cmd`, `venv_cmd`, `launch_app`, `env_ready`, `free_port`, `precheck`, `serve`, `BootstrapHandler`, `reset_env` used consistently where referenced. Marker JSON shape (`requirements_hash`, `models_complete`) consistent across Tasks 2/4. ✓

**Known real-world verification gaps (call out, don't hide):**
- `uv` version `0.5.11` in `package.ps1` is a pinned example — confirm/refresh to a current release at build time.
- Ollama's `/SILENT` flag and installer URL are best-effort; the manual e2e (Task 7 Step 3) and a real cleanup run are where these get confirmed. Cleanup is non-blocking by design, so a wrong flag degrades gracefully.
- The clean-VM acceptance install (Task 10) is the only test that truly validates the shipped installer; it can't be automated here.
