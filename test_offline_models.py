"""Bundled models load in place from a read-only cache with HF_HUB_OFFLINE on (#14).
No network: offline mode makes any Hub request raise, so a pass means none was made."""
import os
import stat

import huggingface_hub
import pytest
from huggingface_hub import constants

import flow


def _cache_repo(hub, repo_id, files):
    """A Hugging Face cache entry shaped like package.ps1 ships it: refs + real snapshot files, no blobs."""
    repo = hub / ("models--" + repo_id.replace("/", "--"))
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("abc123")
    snap = repo / "snapshots" / "abc123"
    for name in files:
        (snap / name).parent.mkdir(parents=True, exist_ok=True)
        (snap / name).write_text("x")
    return snap


def _read_only(root):
    for d, _, files in os.walk(root):
        for f in files:
            os.chmod(os.path.join(d, f), stat.S_IREAD)
        os.chmod(d, stat.S_IREAD | stat.S_IEXEC)


@pytest.fixture
def caches(tmp_path, monkeypatch):
    bundled, home = tmp_path / "bundled" / "hub", tmp_path / "user"
    snaps = {
        "kokoro": _cache_repo(bundled, "hexgrad/Kokoro-82M", ["config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"]),
        "small": _cache_repo(bundled, "Systran/faster-whisper-small", ["model.bin"]),
        "medium": _cache_repo(home / "hub", "Systran/faster-whisper-medium", ["model.bin"]),
    }
    _read_only(bundled)
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(bundled))
    monkeypatch.setattr(constants, "HF_HOME", str(home))
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True)
    yield snaps
    for d, _, _ in os.walk(bundled):
        os.chmod(d, stat.S_IRWXU)   # let pytest clean up


def test_kokoro_files_resolve_from_the_read_only_bundle(caches):
    # Kokoro's own calls: KModel (config + weights) and KPipeline.load_single_voice.
    for name in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
        path = huggingface_hub.hf_hub_download(repo_id="hexgrad/Kokoro-82M", filename=name)
        assert os.path.samefile(path, caches["kokoro"] / name)


def test_whisper_prefers_bundled_then_user_cache(caches):
    assert os.path.samefile(flow.whisper_path("small"), caches["small"])
    assert os.path.samefile(flow.whisper_path("medium"), caches["medium"])


def test_missing_whisper_downloads_into_user_cache_with_offline_lifted(caches, monkeypatch):
    real, calls = huggingface_hub.snapshot_download, []

    def fake(repo_id, **kw):
        if kw.get("local_files_only"):
            return real(repo_id, **kw)
        calls.append((repo_id, kw["cache_dir"], constants.HF_HUB_OFFLINE))
        return "downloaded"

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
    assert flow.whisper_path("large-v3") == "downloaded"
    assert calls == [("Systran/faster-whisper-large-v3", os.path.join(constants.HF_HOME, "hub"), False)]
    assert constants.HF_HUB_OFFLINE is True   # restored after the download
