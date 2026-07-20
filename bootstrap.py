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


SETUP_PORT = 7733


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


import webbrowser

_APP_BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
]


def open_setup_window(url):
    """Open the setup UI as a chromeless app window (Edge/Chrome), not a browser tab.
    Falls back to the default browser if neither is found."""
    profile = DATA_DIR / "setup-ui"
    for raw in _APP_BROWSERS:
        exe = os.path.expandvars(raw)
        if Path(exe).exists():
            try:
                profile.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}"])
                return
            except Exception:
                pass
    webbrowser.open(url)


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
    # single-instance: if setup is already running, surface it instead of starting a 2nd
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", SETUP_PORT)) == 0:
            open_setup_window(f"http://127.0.0.1:{SETUP_PORT}/")
            return
    try:
        srv = serve(SETUP_PORT)
    except OSError:
        srv = serve(free_port(7734))   # port held by something else -> fall back
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.8, lambda: open_setup_window(url)).start()
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
