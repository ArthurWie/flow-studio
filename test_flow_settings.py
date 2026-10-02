"""settings.json round-trip in flow.py. Needs numpy + flask to import flow."""
import json

import flow


def test_settings_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(flow, "settings", dict(flow.settings))
    flow.settings.update(hotkey="ctrl+alt+k", mic_index=3, whisper_row="faster-whisper-medium",
                         cleanup_model="llama3.2", language="de", overlay_style="orb")
    flow.save_settings()
    saved = dict(flow.settings)

    monkeypatch.setattr(flow, "settings", {**saved, "hotkey": "x", "mic_index": None})
    flow.load_settings()
    assert flow.settings == saved


def test_load_settings_ignores_junk(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(flow, "SETTINGS_FILE", f)
    monkeypatch.setattr(flow, "settings", {"hotkey": "ctrl+shift+space"})
    flow.load_settings()                      # missing file
    f.write_text("{not json")
    flow.load_settings()                      # corrupt file
    f.write_text("[1, 2]")
    flow.load_settings()                      # not an object
    f.write_text(json.dumps({"hotkey": "f9", "unknown": 1}))
    flow.load_settings()
    assert flow.settings == {"hotkey": "f9"}
