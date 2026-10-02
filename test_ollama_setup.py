"""In-app Ollama setup: only a validly signed installer may run. Needs numpy + flask to import flow."""
import io
from pathlib import Path

import flow
import os_mac
import os_win


def _fake_install(monkeypatch, signed):
    ran = []
    monkeypatch.setattr(flow.sys, "platform", "win32")
    monkeypatch.setattr(flow, "ollama_up", lambda: False)
    monkeypatch.setattr(flow.shutil, "which", lambda n: None)
    monkeypatch.setattr(flow.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"MZ"))
    monkeypatch.setattr(flow, "osi", os_win)
    monkeypatch.setattr(os_win, "signature_ok", lambda p: signed)
    monkeypatch.setattr(flow.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(flow, "resolve_cleanup_model", lambda: None)
    flow.setup_ollama()
    return ran


def test_unsigned_installer_never_runs(monkeypatch):
    assert _fake_install(monkeypatch, signed=False) == []
    assert "signature" in flow.state["ollama_setup"]


def test_signed_installer_runs_then_pulls(monkeypatch):
    ran = _fake_install(monkeypatch, signed=True)
    assert ran[0][0].endswith("OllamaSetup.exe") and ran[0][1] == "/SILENT"
    assert ran[1][1:] == ["pull", "qwen2.5:3b"]
    assert flow.state["ollama_setup"] == "done"


def test_signature_ok_reads_powershell_status(monkeypatch):
    class R: stdout = "Valid\n"
    monkeypatch.setattr("subprocess.run", lambda *a, **k: R())
    assert os_win.signature_ok("x.exe") is True
    R.stdout = "NotSigned\n"
    assert os_win.signature_ok("x.exe") is False


def _fake_mac_install(monkeypatch, tmp_path, signed):
    ran, up = [], []
    monkeypatch.setattr(flow.sys, "platform", "darwin")
    monkeypatch.setattr(flow, "DATA_DIR", tmp_path)
    monkeypatch.setattr(flow, "MAC_APPS", tmp_path / "Applications")
    monkeypatch.setattr(flow, "ollama_up", lambda: bool(up))
    monkeypatch.setattr(flow, "ollama_exe", lambda: "ollama")
    monkeypatch.setattr(flow.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"PK"))
    monkeypatch.setattr(flow, "osi", os_mac)
    monkeypatch.setattr(os_mac, "signature_ok", lambda p: signed and p.name == "Ollama.app")
    monkeypatch.setattr(flow.time, "sleep", lambda s: None)
    monkeypatch.setattr(flow, "resolve_cleanup_model", lambda: None)

    def run(cmd, **k):
        ran.append(cmd)
        if cmd[0] == "ditto":
            (Path(cmd[-1]) / "Ollama.app").mkdir(parents=True)
        if cmd[0] == "open":
            up.append(1)
    monkeypatch.setattr(flow.subprocess, "run", run)
    flow.setup_ollama()
    return ran


def test_mac_unsigned_app_never_installed(monkeypatch, tmp_path):
    ran = _fake_mac_install(monkeypatch, tmp_path, signed=False)
    assert [c[0] for c in ran] == ["ditto"]
    assert "signature" in flow.state["ollama_setup"]
    assert not (tmp_path / "Applications" / "Ollama.app").exists()
    assert list(tmp_path.iterdir()) == []   # zip + unpacked app cleaned up


def test_mac_signed_app_installed_started_then_pulls(monkeypatch, tmp_path):
    ran = _fake_mac_install(monkeypatch, tmp_path, signed=True)
    app = tmp_path / "Applications" / "Ollama.app"
    assert app.is_dir()
    assert ran[1] == ["open", "-a", str(app)]
    assert ran[2][1:] == ["pull", "qwen2.5:3b"]
    assert flow.state["ollama_setup"] == "done"
