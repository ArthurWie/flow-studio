from pathlib import Path

import paths


def test_data_dir_per_os(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: Path("/h"))
    monkeypatch.setenv("LOCALAPPDATA", "/lad")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    for plat, want in [("win32", "/lad/FlowStudio"),
                       ("darwin", "/h/Library/Application Support/FlowStudio"),
                       ("linux", "/xdg/flow-studio")]:
        monkeypatch.setattr(paths.sys, "platform", plat)
        assert paths.data_dir() == Path(want)
    monkeypatch.delenv("XDG_DATA_HOME")
    assert paths.data_dir() == Path("/h/.local/share/flow-studio")
