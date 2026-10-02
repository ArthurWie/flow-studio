"""In-app Ollama setup: only a validly signed installer may run. Needs numpy + flask to import flow."""
import io

import flow


def _fake_install(monkeypatch, signed):
    ran = []
    monkeypatch.setattr(flow.sys, "platform", "win32")
    monkeypatch.setattr(flow, "ollama_up", lambda: False)
    monkeypatch.setattr(flow.shutil, "which", lambda n: None)
    monkeypatch.setattr(flow.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"MZ"))
    monkeypatch.setattr(flow.osi, "signature_ok", lambda p: signed)
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
    assert flow.osi.signature_ok("x.exe") is True
    R.stdout = "NotSigned\n"
    assert flow.osi.signature_ok("x.exe") is False
