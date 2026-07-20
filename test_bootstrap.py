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
