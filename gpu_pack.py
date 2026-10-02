"""GPU pack (#18): an optional CUDA build of the frozen app for NVIDIA machines. Stdlib only.

The installer ships the CPU build. The pack is two sets of zips on the GitHub release, listed
with their SHA-256 and size in gpu-pack.json:
  core  the CUDA build of the app minus the NVIDIA libraries, versioned with the app
  libs  the NVIDIA libraries (cuBLAS, cuDNN, ...), re-downloaded only when they change
Both are verified before anything is extracted, extracted to a temp folder, renamed into place,
and only then switched on by rewriting pack.json. The CPU build is never touched.

On start, hand_off() in the CPU build starts the pack's exe and exits once that one answers;
if it exits, fails its CUDA check or doesn't answer in time, the CPU build carries on and
the failure is recorded (the model manager shows it, with Retry).
"""
import functools
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from paths import data_dir

RELEASES = "https://github.com/ArthurWie/flow-studio/releases/download"
MANIFEST = "gpu-pack.json"
GPU_DIR = data_dir() / "gpu"
MARKER = GPU_DIR / "pack.json"   # the active pack: {version, core, libs, libs_id[, failed]}
HEALTH_TIMEOUT = 90              # cold CUDA start: torch + cuDNN load from disk
RETRIES = 3                      # per file, each resuming with Range

job = {}                         # the current or last download: state, done, total, error
_lock = threading.Lock()         # one download at a time, whoever starts it


def app_version():
    """The version the build was made with (FlowStudio.spec writes it), "dev" from source."""
    try:
        return (Path(__file__).resolve().parent / "version.txt").read_text().strip()
    except OSError:
        return "dev"


@functools.lru_cache(maxsize=1)
def has_nvidia():
    """An NVIDIA GPU with a driver: nvidia-smi ships with the driver. Windows only for now."""
    if sys.platform != "win32" or not shutil.which("nvidia-smi"):
        return False
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "GPU" in r.stdout


def read_marker():
    try:
        m = json.loads(MARKER.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_marker(m):
    GPU_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MARKER.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    os.replace(tmp, MARKER)       # atomic: a crash leaves the old pack or the new one


def mark_failed(reason):
    m = read_marker()
    if m:
        _write_marker({**m, "failed": reason})
    print(f"  [!] GPU pack: {reason}; running on the CPU")


def status():
    """For the UI: none / ready / outdated (app updated, pack not yet) / failed, plus the download."""
    m = read_marker()
    state = ("none" if not m else "failed" if m.get("failed")
             else "ready" if m.get("version") == app_version() else "outdated")
    return {"state": state, "failed": m.get("failed"), "active": bool(os.environ.get("FLOW_GPU")),
            "job": job_state()}


# ── download + install ─────────────────────────────────────────────────────
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url, dest, size, sha256):
    """Download one file to dest, resuming a .part left by an earlier try with HTTP Range.
    Verified before it's renamed to dest; a checksum mismatch deletes it."""
    if dest.is_file() and dest.stat().st_size == size:
        job["done"] += size
        return
    part = dest.with_name(dest.name + ".part")
    for attempt in range(RETRIES):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        start = job["done"]
        try:
            if have < size:
                req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status != 206:   # server ignored the Range: start over
                        have = 0
                    job["done"] += have
                    with open(part, "ab" if have else "wb") as f:
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                            job["done"] += len(chunk)
                if part.stat().st_size < size:   # urllib ends a cut-short body quietly
                    raise ConnectionError("the connection dropped")
            else:
                job["done"] += have
            break
        except (OSError, http.client.HTTPException):   # URLError, reset, timeout, cut short: resume
            job["done"] = start
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    if part.stat().st_size != size or _sha256(part) != sha256:
        part.unlink()
        raise RuntimeError(f"{dest.name} is corrupt (checksum mismatch), deleted it")
    os.replace(part, dest)


def _extract(zips, name):
    """Extract verified zips into GPU_DIR/name via a temp folder, replacing what was there."""
    tmp, final = GPU_DIR / (name + ".tmp"), GPU_DIR / name
    shutil.rmtree(tmp, ignore_errors=True)
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
    if final.exists():
        old = GPU_DIR / f"{name}.old{int(time.time())}"
        final.rename(old)
        shutil.rmtree(old, ignore_errors=True)
    tmp.rename(final)


def _keep_libs(manifest):
    """The installed libs are the manifest's and the pack didn't fail: don't download them again."""
    m = read_marker()
    return (not m.get("failed") and m.get("libs_id") == manifest["libs_id"]
            and (GPU_DIR / ("libs-" + manifest["libs_id"][:12])).is_dir())


def install(manifest, src):
    """Extract the pack from verified zips in src, then switch it on. Old folders are removed
    best-effort (a running pack's files are locked)."""
    libs = "libs-" + manifest["libs_id"][:12]
    core = "core-" + manifest["version"]
    if not _keep_libs(manifest):
        _extract([src / f["name"] for f in manifest["libs"]], libs)
    _extract([src / f["name"] for f in manifest["core"]], core)
    _write_marker({"version": manifest["version"], "core": core, "libs": libs,
                   "libs_id": manifest["libs_id"]})
    for p in GPU_DIR.iterdir():
        if p.is_dir() and p.name not in (core, libs, "downloads"):
            shutil.rmtree(p, ignore_errors=True)


def download(version=None, base=RELEASES):
    """Download and install the pack for this app version in the background. False if a
    download is already running. Partial files stay in GPU_DIR/downloads, so Retry resumes."""
    if not _lock.acquire(blocking=False):
        return False
    version = version or app_version()
    job.clear()
    job.update(state="downloading", done=0, total=None, error=None, started=time.time())

    def run():
        try:
            url = f"{base}/v{version}"
            with urllib.request.urlopen(f"{url}/{MANIFEST}", timeout=30) as r:
                manifest = json.load(r)
            files = manifest["core"] + ([] if _keep_libs(manifest) else manifest["libs"])
            job["total"] = sum(f["size"] for f in files)
            dl = GPU_DIR / "downloads"
            dl.mkdir(parents=True, exist_ok=True)
            for f in files:
                _fetch(f"{url}/{f['name']}", dl / f["name"], f["size"], f["sha256"])
            job["state"] = "installing"
            install(manifest, dl)
            shutil.rmtree(dl, ignore_errors=True)
            job["state"] = "done"
        except Exception as e:
            job.update(state="error", error=str(e) or type(e).__name__)
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def job_state():
    if not job:
        return None
    elapsed = max(time.time() - job["started"], 1e-6)
    return {k: job[k] for k in ("state", "done", "total", "error")} | {"speed": job["done"] / elapsed}


# ── launcher ───────────────────────────────────────────────────────────────
def _force_cpu():
    try:
        return bool(json.loads((data_dir() / "settings.json").read_text(encoding="utf-8")).get("force_cpu"))
    except (OSError, ValueError, AttributeError):
        return False


def _spawn(exe, env):
    """Start another frozen app from this one. PyInstaller's bootloader leaves its own state
    in the environment and the DLL search path, and the child would pick both up."""
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if sys.platform != "win32":
        return subprocess.Popen([str(exe), *sys.argv[1:]], env=env, cwd=exe.parent)
    import ctypes
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(32768)
    had = k32.GetDllDirectoryW(len(buf), buf)
    k32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen([str(exe), *sys.argv[1:]], env=env, cwd=exe.parent)
    finally:
        k32.SetDllDirectoryW(buf.value if had else None)


def hand_off(ready):
    """In the CPU build: run the GPU pack instead when it's installed for this version, and exit
    once it answers (ready() is true). Returns when this process should carry on, on the CPU."""
    if os.environ.get("FLOW_GPU") or not getattr(sys, "frozen", False) or _force_cpu():
        return
    m = read_marker()
    if m.get("failed") or m.get("version") != app_version():
        return
    exe, libs = GPU_DIR / m["core"] / Path(sys.executable).name, GPU_DIR / m["libs"]
    if not exe.is_file() or not libs.is_dir():
        return mark_failed("its files are missing")
    print("  [i] GPU pack: starting", exe)
    try:
        p = _spawn(exe, dict(os.environ, FLOW_GPU="1", FLOW_GPU_LIBS=str(libs)))
    except OSError as e:
        return mark_failed(f"it didn't start ({e})")
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if p.poll() is not None:
            return mark_failed(f"it exited with code {p.returncode} (see flow_studio_gpu.log)")
        if ready():
            sys.exit(0)   # the GPU build is up and owns the window from here
        time.sleep(0.5)
    p.kill()
    mark_failed(f"it didn't answer within {HEALTH_TIMEOUT} s")


def use_libs():
    """In the GPU build, before torch or ctranslate2 load: find the NVIDIA libs in their own folder."""
    libs = os.environ.get("FLOW_GPU_LIBS")
    if libs and sys.platform == "win32":
        os.add_dll_directory(libs)
        os.environ["PATH"] = libs + os.pathsep + os.environ.get("PATH", "")


def check_cuda():
    """The GPU build's health check: None when torch and ctranslate2 both see a CUDA device."""
    try:
        import torch
        if not torch.cuda.is_available():
            return "torch sees no CUDA device"
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return "ctranslate2 sees no CUDA device"
    except Exception as e:
        return f"CUDA failed to load: {e!r}"
    return None
