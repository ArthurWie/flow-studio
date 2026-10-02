"""Auto-updater (#17): signature + hash checks, resume, the launch check. Needs cryptography to sign."""
import hashlib
import http.server
import os
import sys
import threading
import time

import pytest

import updater

# RFC 8032 §7.1 TEST 1 and TEST 2: (public key, message, signature)
RFC = [("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
       ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")]


@pytest.mark.parametrize("pub,msg,sig", RFC)
def test_verify_rfc8032(pub, msg, sig):
    pub, msg, sig = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
    assert updater.verify(pub, msg, sig)
    assert not updater.verify(pub, msg + b"x", sig)
    assert not updater.verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))


class _Range(http.server.SimpleHTTPRequestHandler):
    """Static files with Range support (SimpleHTTPRequestHandler has none); logs Range headers."""
    ranges = []

    def do_GET(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return self.send_error(404)
        data = open(path, "rb").read()
        rng = self.headers.get("Range")
        self.ranges.append(rng)
        start = int(rng[6:-1]) if rng else 0
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        self.wfile.write(data[start:])

    def log_message(self, *a):
        pass


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A local 'GitHub release' of version 2.0.0 for this OS; the app is 1.0.0."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    key = Ed25519PrivateKey.generate()
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "Setup.bin").write_bytes(os.urandom(300_000))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: _Range(*a, directory=str(rel)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(updater, "ASSET", {sys.platform: "Setup.bin"})
    monkeypatch.setattr(updater, "RELEASES", base)
    monkeypatch.setattr(updater, "MANIFEST_URL", base + "/latest.json")
    monkeypatch.setattr(updater, "PUBLIC_KEY", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    monkeypatch.setattr(updater, "DIR", tmp_path / "update")
    monkeypatch.setattr(updater, "VERSION_FILE", tmp_path / "version.txt")
    monkeypatch.setattr(updater, "status", {"state": "idle"})
    (tmp_path / "version.txt").write_text("1.0.0")
    applied = []
    monkeypatch.setattr(updater, "_apply", applied.append)
    _Range.ranges = []
    raw, sig = updater.build_manifest("2.0.0", "v2.0.0", [rel / "Setup.bin"],
                                      key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex())
    # build_manifest wrote release URLs under /download/<tag>/; serve the file there too.
    (rel / "download" / "v2.0.0").mkdir(parents=True)
    (rel / "download" / "v2.0.0" / "Setup.bin").write_bytes((rel / "Setup.bin").read_bytes())
    (rel / "latest.json").write_bytes(raw)
    (rel / "latest.json.sig").write_text(sig)
    yield rel, applied
    srv.shutdown()


def _wait(state):
    for _ in range(100):
        if updater.status["state"] == state:
            return
        time.sleep(0.05)
    raise AssertionError(updater.status)


def test_update_installs_after_idle(release, monkeypatch):
    rel, applied = release
    idle = threading.Event()
    monkeypatch.setattr(updater, "busy", lambda: not idle.is_set())
    updater.check()
    assert updater.status["state"] == "available" and updater.status["version"] == "2.0.0"
    assert updater.install()
    _wait("ready")
    time.sleep(0.3)
    assert not applied                      # dictating / speaking: no install yet
    idle.set()
    _wait("installing")
    time.sleep(0.1)
    assert len(applied) == 1 and applied[0].read_bytes() == (rel / "Setup.bin").read_bytes()


def test_tampered_manifest_is_rejected(release):
    rel, _ = release
    (rel / "latest.json").write_bytes((rel / "latest.json").read_bytes().replace(b"2.0.0", b"2.0.1"))
    updater.check()
    assert updater.status["state"] == "idle"


def test_tampered_file_is_rejected(release):
    rel, applied = release
    (rel / "download" / "v2.0.0" / "Setup.bin").write_bytes(os.urandom(300_000))
    updater.check()
    updater.install()
    _wait("error")
    assert "SHA-256" in updater.status["error"] and not applied
    assert not list(updater.DIR.glob("*"))  # the bad bytes are gone, not resumed from


def test_partial_download_resumes_on_next_launch(release):
    rel, applied = release
    data = (rel / "Setup.bin").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    updater.DIR.mkdir()
    (updater.DIR / f"{sha}.part").write_bytes(data[:100_000])   # killed mid-download
    (updater.DIR / "old.part").write_bytes(b"stale")
    updater.check()                                             # next launch: resumes by itself
    _wait("installing")
    time.sleep(0.1)
    assert _Range.ranges[-1] == "bytes=100000-"
    assert applied[0].read_bytes() == data
    assert not (updater.DIR / "old.part").exists()


def test_no_check_in_a_dev_run(release, monkeypatch):
    monkeypatch.setattr(updater, "VERSION_FILE", updater.DIR / "missing.txt")
    updater.check()
    assert _Range.ranges == [] and updater.status["state"] == "idle"
