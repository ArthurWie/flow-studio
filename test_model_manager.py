"""Model manager (#16): status per cache, a download with byte progress, Retry after a failure,
delete only for downloaded models. No network: snapshot_download is faked the way hub 1.33
drives its tqdm_class, and offline mode would make any real request raise."""
import os
import threading
import time
from pathlib import Path

import huggingface_hub
import pytest
from huggingface_hub import constants

import flow
import model_manager as mm
from test_offline_models import WHISPER, _cache_repo, caches  # noqa: F401 (fixture)


def _wait(cond, timeout=5):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "timed out"
        time.sleep(0.01)


def test_status_per_cache(caches):
    assert mm.find("whisper/small")[0] == "bundled"
    assert mm.find("whisper/medium")[0] == "downloaded"
    assert mm.find("whisper/large-v3") == ("not downloaded", None)
    assert mm.find("kokoro")[0] == "bundled"
    assert mm.find("kokoro/af_heart")[0] == "bundled"
    assert mm.find("kokoro/am_adam")[0] == "not downloaded"
    assert not mm.deletable("whisper/small") and mm.deletable("whisper/medium")


def test_partial_download_does_not_count(caches):
    repo = caches["medium"].parent.parent
    (repo / "blobs").mkdir()
    (repo / "blobs" / "abc.1234.incomplete").write_text("x")
    assert mm.find("whisper/medium")[0] == "not downloaded"
    (repo / "blobs" / "abc.1234.incomplete").unlink()
    (caches["medium"] / "model.bin").unlink()
    assert mm.find("whisper/medium")[0] == "not downloaded"


def test_unfrozen_cache_is_downloaded_not_bundled(caches, monkeypatch):
    monkeypatch.setattr(constants, "HF_HUB_CACHE", os.path.join(constants.HF_HOME, "hub"))
    assert mm.find("whisper/medium")[0] == "downloaded"
    assert mm.find("whisper/small")[0] == "not downloaded"


def _fake_hub(monkeypatch, go, fail=False):
    """snapshot_download as hub 1.33 runs it: one aggregated "Reconstructing" byte bar fed per file."""
    seen = {}

    def fake(repo_id, allow_patterns, cache_dir, tqdm_class):
        seen.update(repo_id=repo_id, cache_dir=cache_dir, offline=constants.HF_HUB_OFFLINE)
        transfer = tqdm_class(desc="Downloading bytes", total=0, initial=0, unit="B", unit_scale=True)
        bar = tqdm_class(desc="Reconstructing (incomplete total...)", total=0, initial=0, unit="B", unit_scale=True)
        bar.total += 1000
        for _ in range(4):
            bar.update(100)
            transfer.update(90)
        go.wait(5)
        if fail:
            raise OSError("connection reset")
        bar.update(600)
        _cache_repo(Path(cache_dir), repo_id, WHISPER)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
    return seen


def test_download_reports_bytes_and_selects_the_model(caches, monkeypatch):
    go, done = threading.Event(), []
    seen = _fake_hub(monkeypatch, go)
    assert mm.download("whisper/large-v3", on_done=done.append)
    _wait(lambda: (mm.job_state() or {}).get("done") == 400)
    st = mm.job_state()
    assert st["state"] == "downloading" and st["total"] == 1000 and st["speed"] > 0
    assert not mm.download("whisper/medium")          # one at a time
    go.set()
    _wait(lambda: mm.job_state()["state"] != "downloading")
    assert mm.job_state()["state"] == "done" and mm.job_state()["done"] == 1000
    assert done == ["whisper/large-v3"]
    assert seen == {"repo_id": "Systran/faster-whisper-large-v3",
                    "cache_dir": os.path.join(constants.HF_HOME, "hub"), "offline": False}
    assert constants.HF_HUB_OFFLINE is True           # offline again afterwards
    assert mm.find("whisper/large-v3")[0] == "downloaded"


def test_failed_download_keeps_the_error_and_allows_retry(caches, monkeypatch):
    go, done = threading.Event(), []
    _fake_hub(monkeypatch, go, fail=True)
    go.set()
    assert mm.download("whisper/large-v3", on_done=done.append)
    _wait(lambda: mm.job_state()["state"] != "downloading")
    assert mm.job_state()["state"] == "error" and "connection reset" in mm.job_state()["error"]
    assert done == [] and mm.find("whisper/large-v3")[0] == "not downloaded"
    _fake_hub(monkeypatch, go)
    assert mm.download("whisper/large-v3", on_done=done.append)   # Retry: the lock was released
    _wait(lambda: mm.job_state()["state"] != "downloading")
    assert done == ["whisper/large-v3"]


def test_delete_only_downloaded(caches):
    with pytest.raises(ValueError):
        mm.delete("whisper/small")          # bundled
    with pytest.raises(ValueError):
        mm.delete("kokoro/af_heart")
    # A real hub layout: snapshot files link into blobs/.
    repo = caches["medium"].parent.parent
    (repo / "blobs").mkdir()
    for name in WHISPER:
        (repo / "blobs" / name).write_text("x")
        (caches["medium"] / name).unlink()
        (caches["medium"] / name).symlink_to(os.path.join("..", "..", "blobs", name))
    assert mm.find("whisper/medium")[0] == "downloaded"
    mm.delete("whisper/medium")
    assert not repo.exists() and mm.find("whisper/medium")[0] == "not downloaded"


def test_api_only_selects_installed_models(caches, monkeypatch):
    monkeypatch.setattr(flow, "settings", dict(flow.settings, whisper_row="faster-whisper-small"))
    monkeypatch.setattr(flow, "save_settings", lambda: None)
    c = flow.app.test_client()
    assert c.post("/api/model", json={"row": "faster-whisper-large-v3"}).json["whisper_row"] == "faster-whisper-small"
    assert c.post("/api/model", json={"row": "faster-whisper-medium"}).json["whisper_row"] == "faster-whisper-medium"
    rows = {r["key"]: r for r in c.get("/api/models").json["models"]}
    assert rows["whisper/small"]["status"] == "bundled" and not rows["whisper/small"]["deletable"]
    assert rows["whisper/medium"]["deletable"]
    assert c.post("/api/models/delete", json={"key": "whisper/small"}).status_code == 400
    assert c.post("/api/models/download", json={"key": "../../etc"}).status_code == 400
