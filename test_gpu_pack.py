"""GPU pack (#18): download with Range resume and SHA-256, install + atomic switch, the launcher's
CPU fallback. A local HTTP server stands in for GitHub Releases; fake exes for the CUDA build."""
import hashlib
import json
import os
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import gpu_pack


class Releases(BaseHTTPRequestHandler):
    """Serves files from `root` with Range support; `cut` drops the first response of a file
    after that many bytes (a lost connection)."""
    root, cut, ranges = None, {}, []

    def do_GET(self):
        f = self.root / self.path.rsplit("/", 1)[-1]
        if not f.is_file():
            return self.send_error(404)
        data, start = f.read_bytes(), 0
        if rng := self.headers.get("Range"):
            Releases.ranges.append((f.name, rng))
            start = int(rng.split("=")[1].rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if (n := Releases.cut.pop(f.name, None)) is not None:
            self.wfile.write(body[:n])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """A release v1.0.0 with one core zip and one libs zip, served on localhost; gpu dir in tmp."""
    rel = tmp_path / "v1.0.0"
    rel.mkdir()

    def zipped(name, files):
        z = rel / name
        with zipfile.ZipFile(z, "w") as zf:
            for arc, data in files.items():
                zf.writestr(arc, data)
        return {"name": name, "size": z.stat().st_size, "sha256": hashlib.sha256(z.read_bytes()).hexdigest()}

    manifest = {"version": "1.0.0", "libs_id": "ab" * 32,
                "core": [zipped("core-1.zip", {"FlowStudio": "exe", "_internal/x.pyd": os.urandom(200_000)})],
                "libs": [zipped("libs-1.zip", {"cublas64_12.dll": os.urandom(300_000)})]}
    (rel / gpu_pack.MANIFEST).write_text(json.dumps(manifest))
    Releases.root, Releases.cut, Releases.ranges = rel, {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Releases)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(gpu_pack, "GPU_DIR", tmp_path / "gpu")
    monkeypatch.setattr(gpu_pack, "MARKER", tmp_path / "gpu" / "pack.json")
    monkeypatch.setattr(gpu_pack, "app_version", lambda: "1.0.0")
    monkeypatch.setattr(gpu_pack.time, "sleep", lambda s: None)
    yield {"base": f"http://127.0.0.1:{srv.server_port}", "rel": rel, "manifest": manifest}
    srv.shutdown()


def _download(base):
    assert gpu_pack.download(base=base)
    deadline = time.time() + 10
    while gpu_pack.job["state"] in ("downloading", "installing"):
        assert time.time() < deadline, "timed out"
        time.sleep(0.01)
    return gpu_pack.job_state()


def test_download_installs_and_switches_on(pack):
    j = _download(pack["base"])
    assert j["state"] == "done" and j["done"] == j["total"] == sum(
        f["size"] for f in pack["manifest"]["core"] + pack["manifest"]["libs"])
    m = gpu_pack.read_marker()
    assert (gpu_pack.GPU_DIR / m["core"] / "FlowStudio").read_text() == "exe"
    assert (gpu_pack.GPU_DIR / m["libs"] / "cublas64_12.dll").is_file()
    assert gpu_pack.status()["state"] == "ready"
    assert not (gpu_pack.GPU_DIR / "downloads").exists()

    j = _download(pack["base"])   # same libs: only the core comes down again
    assert j["state"] == "done" and j["total"] == pack["manifest"]["core"][0]["size"]


def test_dropped_connection_resumes_with_range(pack):
    Releases.cut["libs-1.zip"] = 100_000
    j = _download(pack["base"])
    assert j["state"] == "done", j["error"]
    assert ("libs-1.zip", "bytes=100000-") in Releases.ranges


def test_retry_resumes_a_part_left_by_an_earlier_try(pack):
    dl = gpu_pack.GPU_DIR / "downloads"
    dl.mkdir(parents=True)
    (dl / "core-1.zip.part").write_bytes((pack["rel"] / "core-1.zip").read_bytes()[:5000])
    assert _download(pack["base"])["state"] == "done"
    assert ("core-1.zip", "bytes=5000-") in Releases.ranges


def test_corrupt_archive_is_never_extracted(pack):
    gpu_pack._write_marker({"version": "0.9.0", "core": "core-0.9.0", "libs": "x", "libs_id": "y"})
    before = gpu_pack.read_marker()
    z = pack["rel"] / "libs-1.zip"
    z.write_bytes(z.read_bytes()[:-1] + b"X")   # same size, wrong bytes
    j = _download(pack["base"])
    assert j["state"] == "error" and "checksum" in j["error"]
    assert gpu_pack.read_marker() == before            # old pack untouched, CPU keeps working
    assert not any(gpu_pack.GPU_DIR.glob("libs-*"))
    assert not list((gpu_pack.GPU_DIR / "downloads").glob("libs-1.zip*"))   # the bad file is gone


def test_network_off_is_an_error_with_retry(pack):
    j = _download("http://127.0.0.1:9")   # nothing listens on the discard port
    assert j["state"] == "error" and j["error"]
    assert gpu_pack.status()["state"] == "none"
    assert _download(pack["base"])["state"] == "done"   # Retry


def test_one_download_at_a_time(pack):
    assert gpu_pack._lock.acquire(blocking=False)
    try:
        assert gpu_pack.download(base=pack["base"]) is False
    finally:
        gpu_pack._lock.release()


# ── launcher ───────────────────────────────────────────────────────────────
@pytest.fixture
def installed(pack, monkeypatch):
    """An installed pack whose exe is a shell script; this process poses as the frozen CPU build."""
    _download(pack["base"])
    exe = gpu_pack.GPU_DIR / gpu_pack.read_marker()["core"] / "FlowStudio"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/cpu/FlowStudio")
    monkeypatch.delenv("FLOW_GPU", raising=False)
    monkeypatch.setattr(gpu_pack, "_force_cpu", lambda: False)

    def script(body):
        exe.write_text(f"#!/bin/sh\n{body}\n")
        exe.chmod(0o755)
    return script


pytestmark_unix = pytest.mark.skipif(sys.platform == "win32", reason="fake exe is a shell script")


@pytestmark_unix
def test_hands_off_once_the_pack_answers(installed, tmp_path):
    installed(f'echo "$FLOW_GPU $PYINSTALLER_RESET_ENVIRONMENT" > {tmp_path}/env; sleep 5')
    seen = tmp_path / "env"
    with pytest.raises(SystemExit) as e:
        gpu_pack.hand_off(seen.exists)
    assert e.value.code == 0
    assert seen.read_text().split() == ["1", "1"]


@pytestmark_unix
def test_pack_that_exits_falls_back_to_cpu(installed):
    installed("exit 3")   # e.g. its CUDA check failed
    assert gpu_pack.hand_off(lambda: False) is None
    assert "code 3" in gpu_pack.read_marker()["failed"]
    assert gpu_pack.status()["state"] == "failed"


@pytestmark_unix
def test_pack_that_hangs_is_killed(installed, monkeypatch):
    installed("sleep 30")
    monkeypatch.setattr(gpu_pack, "HEALTH_TIMEOUT", 0.5)
    gpu_pack.hand_off(lambda: False)
    assert "didn't answer" in gpu_pack.read_marker()["failed"]


def test_deleted_pack_falls_back_to_cpu(installed):
    import shutil
    shutil.rmtree(gpu_pack.GPU_DIR / gpu_pack.read_marker()["core"])
    gpu_pack.hand_off(lambda: pytest.fail("nothing should start"))
    assert "missing" in gpu_pack.read_marker()["failed"]


def test_stays_on_cpu_when_outdated_failed_or_forced(installed, monkeypatch):
    never = lambda: pytest.fail("nothing should start")   # noqa: E731
    monkeypatch.setattr(gpu_pack, "app_version", lambda: "1.1.0")
    gpu_pack.hand_off(never)
    assert gpu_pack.status()["state"] == "outdated"
    monkeypatch.setattr(gpu_pack, "app_version", lambda: "1.0.0")
    monkeypatch.setattr(gpu_pack, "_force_cpu", lambda: True)
    gpu_pack.hand_off(never)
    monkeypatch.setattr(gpu_pack, "_force_cpu", lambda: False)
    gpu_pack.mark_failed("earlier start failed")
    gpu_pack.hand_off(never)
    assert "failed" in gpu_pack.read_marker()
    # a fresh install (Retry) clears the failure
    gpu_pack.install(json.loads((Path(Releases.root) / gpu_pack.MANIFEST).read_text()),
                     Path(Releases.root))
    assert "failed" not in gpu_pack.read_marker()
