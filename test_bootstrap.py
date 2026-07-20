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
