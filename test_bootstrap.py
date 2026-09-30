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


def test_read_marker_corrupt_json_returns_empty(tmp_path):
    marker = tmp_path / ".setup_complete"
    marker.write_bytes(b"not json{")
    assert bs.read_marker(marker) == {}


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
    assert bs.venv_cmd() == [str(tmp_path / "uv.exe"), "venv",
                             "--python", "3.12", str(tmp_path / "env")]


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


def test_app_env_isolates_uv_python(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path / "data")
    env = bs.app_env()
    assert env["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "data" / "uv-python")
    assert env["UV_PYTHON_PREFERENCE"] == "managed"


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
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(bs, "precheck", lambda: [])
    bs.run_setup(runner=lambda cmd, env: 0)     # all steps succeed
    assert bs.read_marker(marker).get("models_complete") is True


def test_run_setup_no_marker_on_failure(monkeypatch, tmp_path):
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "MARKER", marker)
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(bs, "precheck", lambda: [])
    bs.run_setup(runner=lambda cmd, env: 1)     # everything fails
    assert not marker.exists()


def test_run_setup_no_marker_on_partial_failure(monkeypatch, tmp_path):
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "MARKER", marker)
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(bs, "precheck", lambda: [])
    def runner(cmd, env):
        return 1 if "-c" in cmd else 0     # only the models/warm step fails
    bs.run_setup(runner=runner)
    assert not marker.exists()
    assert bs.state["steps"]["models"] == "error"


def test_run_setup_aborts_when_precheck_fails(monkeypatch, tmp_path):
    marker = tmp_path / ".setup_complete"
    monkeypatch.setattr(bs, "MARKER", marker)
    monkeypatch.setattr(bs, "ENV_DIR", tmp_path / "env")
    monkeypatch.setattr(bs, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(bs, "precheck", lambda: ["Not enough free disk space."])
    ran = []
    bs.run_setup(runner=lambda cmd, env: ran.append(cmd) or 0)
    assert ran == []                       # no install commands ran
    assert not marker.exists()             # no marker written
    assert bs.state["steps"]["engine"] == "error"
    assert "disk" in bs.state["error"].lower()


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


import json
import threading
import urllib.request
import bootstrap as bs


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).read().decode()


def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    return urllib.request.urlopen(req, timeout=3).read().decode()


def test_ollama_exe_prefers_path(monkeypatch):
    monkeypatch.setattr(bs.shutil, "which", lambda n: "C:/somewhere/ollama.exe")
    assert bs.ollama_exe() == "C:/somewhere/ollama.exe"


def test_ollama_exe_falls_back_to_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(bs.shutil, "which", lambda n: None)
    prog = tmp_path / "Programs" / "Ollama"; prog.mkdir(parents=True)
    (prog / "ollama.exe").write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert bs.ollama_exe().endswith("Ollama\\ollama.exe") or bs.ollama_exe().endswith("Ollama/ollama.exe")


def test_ollama_pull_cmd_uses_resolved_exe(monkeypatch):
    monkeypatch.setattr(bs, "ollama_exe", lambda: "OLLAMA")
    assert bs.ollama_pull_cmd() == ["OLLAMA", "pull", "qwen2.5:3b"]


def test_verify_signature_true_on_valid(monkeypatch):
    class R: stdout = "Valid\n"
    monkeypatch.setattr(bs.subprocess, "run", lambda *a, **k: R())
    assert bs._verify_signature("x.exe") is True


def test_verify_signature_false_on_unsigned(monkeypatch):
    class R: stdout = "NotSigned\n"
    monkeypatch.setattr(bs.subprocess, "run", lambda *a, **k: R())
    assert bs._verify_signature("x.exe") is False


def test_reset_env_clears_dir_and_state(monkeypatch, tmp_path):
    envdir = tmp_path / "env"; envdir.mkdir(); (envdir / "x").write_text("y")
    monkeypatch.setattr(bs, "ENV_DIR", envdir)
    bs.state["steps"]["engine"] = "done"; bs.state["status"] = "done"
    bs.reset_env()
    assert not envdir.exists()
    assert bs.state["steps"]["engine"] == "pending"
    assert bs.state["status"] == "idle"
