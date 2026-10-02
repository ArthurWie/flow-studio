"""Model manager (#16): where each model is, downloading it with byte progress, deleting it.

Bundled models sit read-only in HF_HUB_CACHE (inside the frozen app); downloads go to the user
cache, HF_HOME/hub in the data dir. Keys: "whisper/<size>", "kokoro" (the base model) and
"kokoro/<voice>". Resume is huggingface_hub's own: finished files are kept and a dropped
connection is retried with Range inside one call, but hub 1.x discards a partial file once
the download fails, so Retry restarts that file.
"""
import fnmatch
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

KOKORO_REPO = "hexgrad/Kokoro-82M"
# faster_whisper.utils.download_model's allow_patterns; the need list is what every size has.
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
WHISPER_NEED = ["config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
# Download size shown before a model is on disk (then the real size on disk is shown).
APPROX_SIZE = {"whisper/small": 484_000_000, "whisper/medium": 1_530_000_000,
               "whisper/large-v3": 3_090_000_000, "kokoro": 327_000_000}
VOICE_SIZE = 523_000

job = {}                       # the current or last download: key, state, done, total, error
_lock = threading.Lock()       # one download at a time


def spec(key):
    """(repo_id, allow_patterns, files that must be present) for a model key."""
    tool, _, name = key.partition("/")
    if tool == "whisper" and name:
        return f"Systran/faster-whisper-{name}", WHISPER_FILES, WHISPER_NEED
    if key == "kokoro":
        files = ["config.json", "kokoro-v1_0.pth"]
        return KOKORO_REPO, files, files
    if tool == "kokoro" and name:
        files = [f"voices/{name}.pt"]
        return KOKORO_REPO, files, files
    raise KeyError(key)


def caches():
    """(bundled cache or None, user cache). Unfrozen, HF_HUB_CACHE is the user cache: nothing is bundled."""
    from huggingface_hub import constants
    user = Path(constants.HF_HOME) / "hub"
    bundled = Path(constants.HF_HUB_CACHE)
    return (None if bundled.resolve() == user.resolve() else bundled), user


def _repo_dir(cache, repo_id):
    return Path(cache) / ("models--" + repo_id.replace("/", "--"))


def _files(snap, patterns):
    """The snapshot files matching the patterns (is_file follows links, so a dangling one is absent)."""
    return [p for p in snap.rglob("*") if p.is_file()
            and any(fnmatch.fnmatch(p.relative_to(snap).as_posix(), pat) for pat in patterns)]


def _snapshot(cache, key):
    """The snapshot folder holding the model, only if all its files are there and no blob is mid-download."""
    repo_id, _, need = spec(key)
    repo = _repo_dir(cache, repo_id)
    try:
        snap = repo / "snapshots" / (repo / "refs" / "main").read_text().strip()
    except OSError:
        return None
    names = [p.relative_to(snap).as_posix() for p in _files(snap, need)]
    if any(repo.glob("blobs/*.incomplete")) or not all(fnmatch.filter(names, pat) for pat in need):
        return None
    return snap


def find(key):
    """(status, snapshot folder or None); status is bundled, downloaded or not downloaded. No network."""
    bundled, user = caches()
    if bundled and (snap := _snapshot(bundled, key)):
        return "bundled", snap
    if snap := _snapshot(user, key):
        return "downloaded", snap
    return "not downloaded", None


def size(key, snap=None):
    """Bytes on disk when installed, else the approximate download size."""
    if snap:
        return sum(p.stat().st_size for p in _files(snap, spec(key)[1]))
    return APPROX_SIZE.get(key, VOICE_SIZE)


@contextmanager
def hf_online():
    """Lift HF_HUB_OFFLINE for one user-started download (huggingface_hub reads it per request).
    ponytail: process-wide flag, so a model load racing the download may check revisions too."""
    from huggingface_hub import constants
    was, constants.HF_HUB_OFFLINE = constants.HF_HUB_OFFLINE, False
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = was


def _progress_class():
    """A tqdm for snapshot_download that feeds `job`. Hub 1.x sums every file's bytes into one
    "Reconstructing" bar (bytes written to disk); the bars are disabled, so count here."""
    from tqdm.auto import tqdm

    class Progress(tqdm):
        def __init__(self, *args, **kwargs):
            self._bytes = kwargs.get("unit") == "B" and str(kwargs.get("desc", "")).startswith("Reconstructing")
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            if self._bytes:
                job["bar"] = self

        def update(self, n=1):
            if self._bytes:
                job["done"] += n or 0
            return super().update(n)

    return Progress


def job_state():
    """The download as the UI shows it: bytes done / total and the average speed so far."""
    if not job:
        return None
    bar = job.get("bar")
    elapsed = max(time.time() - job["started"], 1e-6)
    return {"key": job["key"], "state": job["state"], "error": job["error"], "done": job["done"],
            "total": (bar.total if bar else 0) or None, "speed": job["done"] / elapsed}


def download(key, on_done=None):
    """Start downloading a model into the user cache in the background. False if one is running.
    A failure leaves whatever was in use untouched; the job keeps the error for a Retry."""
    if not _lock.acquire(blocking=False):
        return False
    job.clear()
    job.update(key=key, state="downloading", done=0, bar=None, error=None, started=time.time())

    def run():
        try:
            from huggingface_hub import snapshot_download
            repo_id, patterns, _ = spec(key)
            with hf_online():
                snapshot_download(repo_id, allow_patterns=patterns, cache_dir=str(caches()[1]),
                                  tqdm_class=_progress_class())
            if not find(key)[1]:
                raise RuntimeError("the download finished but files are missing")
            job["state"] = "done"
            if on_done:
                on_done(key)
        except Exception as e:
            job.update(state="error", error=str(e) or type(e).__name__)
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def deletable(key):
    """Only a downloaded Whisper size: it owns its repo. Kokoro's voices share one repo and all ship bundled.
    ponytail: per-file delete for Kokoro voices once some aren't bundled."""
    return key.startswith("whisper/") and find(key)[0] == "downloaded"


def delete(key):
    """Remove a downloaded model's repo from the user cache through hub's own cache cleanup, which
    also sweeps hub 1.x's shared blob store. Bundled models can't be deleted."""
    if not deletable(key):
        raise ValueError(f"{key} can't be deleted, only downloaded Whisper models can")
    from huggingface_hub import scan_cache_dir
    cache = scan_cache_dir(caches()[1])
    revs = [r.commit_hash for repo in cache.repos if repo.repo_id == spec(key)[0] for r in repo.revisions]
    cache.delete_revisions(*revs).execute()
    shutil.rmtree(_repo_dir(caches()[1], spec(key)[0]), ignore_errors=True)   # refs/ and leftovers
