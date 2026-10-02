"""Auto-updater (#17): a signed latest.json on GitHub Releases → verified, resumable download →
silent install → relaunch. The app side is stdlib only.

  latest.json      {"version": "1.2.0", "assets": {"win32": {"url", "sha256", "size"}, "darwin": {...}, "linux": {...}}}
  latest.json.sig  hex ed25519 signature of latest.json's exact bytes (PUBLIC_KEY below verifies it)

CI builds and signs both (see __main__).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

import gpu_pack
from paths import data_dir

RELEASES = "https://github.com/ArthurWie/flow-studio/releases"
MANIFEST_URL = RELEASES + "/latest/download/latest.json"
# Hex ed25519 public key; the private half is the UPDATE_SIGNING_KEY repo secret.
# Make the pair with `uv run --with cryptography python updater.py keygen`. Empty = updates off.
PUBLIC_KEY = ""
ASSET = {"win32": "FlowStudioSetup.exe", "darwin": "FlowStudio.dmg",   # release file per OS
         "linux": "flow-studio-linux-x86_64.tar.gz"}
DIR = data_dir() / "update"

status = {"state": "idle"}   # idle | available | downloading | ready | installing | error, + version/done/total/error
busy = lambda: False         # flow_studio sets this: True while dictating or speaking
_lock = threading.Lock()     # one install at a time


# ── ed25519 verify, RFC 8032 §6 (stdlib has no ed25519; verify is all the app needs) ──
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, D = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return E * F, G * H, F * G, E * H


def _mul(s, pt):
    acc = (0, 1, 1, 0)
    while s:
        if s & 1:
            acc = _add(acc, pt)
        pt, s = _add(pt, pt), s >> 1
    return acc


def _point(y, sign):
    """The curve point with this y and x parity, or None if there's none."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P or (x == 0 and sign):
        return None
    if x & 1 != sign:
        x = _P - x
    return x, y, 1, x * y % _P


def _decode(b):
    y = int.from_bytes(b, "little")
    return _point(y & (1 << 255) - 1, y >> 255)


_G = _point(4 * pow(5, _P - 2, _P) % _P, 0)


def verify(public, msg, sig):
    if len(public) != 32 or len(sig) != 64:
        return False
    A, R, s = _decode(public), _decode(sig[:32]), int.from_bytes(sig[32:], "little")
    if A is None or R is None or s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + public + msg).digest(), "little") % _Q
    lhs, rhs = _mul(s, _G), _add(R, _mul(h, A))
    return (lhs[0] * rhs[2] - rhs[0] * lhs[2]) % _P == 0 and (lhs[1] * rhs[2] - rhs[1] * lhs[2]) % _P == 0


# ── app side ──
def current():
    """The running frozen app's version, or None in a dev run (no version.txt: never updates)."""
    v = gpu_pack.app_version()
    return None if v == "dev" else v


def _v(version):
    return tuple(int(x) for x in version.split("."))


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def check():
    """The launch check (run in a thread). Offers a newer signed release; resumes a download the
    user started before the app was closed. Failures are only logged: offline is normal."""
    ver = current()
    if not ver or not PUBLIC_KEY:
        return
    try:
        raw = _get(MANIFEST_URL)
        if not verify(bytes.fromhex(PUBLIC_KEY), raw, bytes.fromhex(_get(MANIFEST_URL + ".sig").decode().strip())):
            raise ValueError("latest.json has a bad signature")
        m = json.loads(raw)
        asset = m["assets"].get(sys.platform)
        newer = asset and _v(m["version"]) > _v(ver)
        for f in DIR.glob("*"):   # downloads of other versions, the installer that just ran
            if not (newer and f.name.startswith(asset["sha256"])):
                with suppress(OSError):   # Windows: the installer may still be running
                    shutil.rmtree(f) if f.is_dir() else f.unlink()   # dir: an unpacked Linux tarball
        if newer:
            status.update(state="available", version=m["version"], asset=asset)
            if any(DIR.glob(asset["sha256"] + "*")):
                install()
    except Exception as e:
        print(f"[update] check failed: {e!r}")


def _download(asset):
    """The verified file for the asset, resuming a partial download (gpu_pack's fetch). Raises on a bad hash."""
    DIR.mkdir(parents=True, exist_ok=True)
    final = DIR / f"{asset['sha256']}-{Path(asset['url']).name}"
    status.update(state="downloading", done=0, total=asset["size"])
    gpu_pack._fetch(asset["url"], final, asset["size"], asset["sha256"], progress=status)
    return final


def install():
    """Download (with progress) → wait until dictation and TTS are idle → install and relaunch.
    False if an install is already running."""
    if status.get("state") not in ("available", "error") or not _lock.acquire(blocking=False):
        return False

    def run():
        try:
            f = _download(status["asset"])
            status["state"] = "ready"
            while busy():   # ponytail: polled, so a dictation started in the same second can still be cut
                time.sleep(1)
            status["state"] = "installing"
            _apply(f)
        except Exception as e:
            status.update(state="error", error=str(e) or type(e).__name__)
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def _apply(f):
    """Hand over to the new version and exit."""
    if sys.platform == "win32":
        # The installer replaces the app once we've exited, then starts it (/RELAUNCH=1, see FlowStudio.iss).
        subprocess.Popen([str(f), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    elif sys.platform == "darwin":
        _swap_app(f)
    elif sys.platform == "linux":
        _reinstall(f)
    else:
        raise RuntimeError("no installer for this OS yet")
    os._exit(0)


def _swap_app(dmg):
    """Copy the new .app next to the running one, swap the two in one atomic rename, and leave a
    shell to delete the old one and open the new one once this process is gone."""
    import ctypes
    import tempfile
    app = Path(sys.executable).resolve().parents[2]   # …/Flow Studio.app/Contents/MacOS/FlowStudio
    new = app.with_name(f".{app.name}.new")
    subprocess.run(["rm", "-rf", str(new)], check=True)
    mnt = tempfile.mkdtemp()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, str(dmg)], check=True)
    try:
        subprocess.run(["ditto", f"{mnt}/Flow Studio.app", str(new)], check=True)
    finally:
        subprocess.run(["hdiutil", "detach", mnt])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renamex_np(os.fsencode(new), os.fsencode(app), 2):   # RENAME_SWAP
        raise OSError(ctypes.get_errno(), f"couldn't swap in the new {app.name}")
    subprocess.Popen(["/bin/sh", "-c", 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; rm -rf "$1"; open "$2"',
                      str(os.getpid()), str(new), str(app)], start_new_session=True)


def _reinstall(tarball):
    """Unpack the new tarball and run its install-linux.sh (it copies next to the old app and
    swaps, so a failure leaves this version installed), then leave a shell to delete the unpacked
    copy and start the new version once this process is gone."""
    import tarfile
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=DIR))
    try:
        with tarfile.open(tarball) as t:
            t.extractall(tmp, filter="data")
        subprocess.run([str(tmp / "flow-studio" / "install-linux.sh")], check=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    subprocess.Popen(["/bin/sh", "-c", 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; rm -rf "$1"; exec "$2"',
                      str(os.getpid()), str(tmp), str(Path.home() / ".local" / "bin" / "flow-studio")],
                     start_new_session=True, stdin=subprocess.DEVNULL)


def ui_state():
    """The update as the dashboard shows it."""
    return {k: v for k, v in status.items() if k != "asset"}


# ── release side (CI): python updater.py manifest <version> <tag> <files…> ──
def build_manifest(version, tag, files, private_hex):
    """(latest.json bytes, hex signature) for the release files, keyed by OS via ASSET."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os_of = {name: plat for plat, name in ASSET.items()}
    assets = {}
    for f in map(Path, files):
        with open(f, "rb") as fh:
            sha = hashlib.file_digest(fh, "sha256").hexdigest()
        assets[os_of[f.name]] = {"url": f"{RELEASES}/download/{tag}/{f.name}", "sha256": sha, "size": f.stat().st_size}
    raw = json.dumps({"version": version, "assets": assets}, indent=2).encode()
    return raw, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex)).sign(raw).hex()


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["keygen"]:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
        k = Ed25519PrivateKey.generate()
        print("UPDATE_SIGNING_KEY (repo secret):", k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex())
        print("PUBLIC_KEY (updater.py):         ", k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    elif cmd == ["manifest"]:
        raw, sig = build_manifest(sys.argv[2], sys.argv[3], sys.argv[4:], os.environ["UPDATE_SIGNING_KEY"])
        Path("latest.json").write_bytes(raw)
        Path("latest.json.sig").write_text(sig)
    else:
        sys.exit("usage: updater.py keygen | manifest <version> <tag> <files…>")
