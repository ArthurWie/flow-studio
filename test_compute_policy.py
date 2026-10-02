"""app.py compute policy: Kokoro device choice, CPU thread cap, background priority.
Fakes torch and kokoro, so it runs without either installed."""

import os
import sys
import threading
import types

import pytest

import app


@pytest.fixture
def fake(monkeypatch):
    calls = {"devices": [], "threads": None, "fail": set()}
    torch = types.ModuleType("torch")
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: calls["mps"]))
    torch.cuda = types.SimpleNamespace(is_available=lambda: calls.get("cuda", False))
    torch.set_num_threads = lambda n: calls.__setitem__("threads", n)

    def KPipeline(lang_code, device):
        calls["devices"].append(device)
        if device in calls["fail"]:
            raise RuntimeError("MPS Error")
        return device

    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=KPipeline))
    monkeypatch.setattr(app, "_pipelines", {})
    monkeypatch.setattr(app, "_device", None)
    monkeypatch.delenv("FLOW_GPU", raising=False)
    return calls


def test_cuda_only_in_the_gpu_pack(fake, monkeypatch):
    fake.update(mps=False, cuda=True)
    assert app.get_pipeline("a") == "cpu"   # the CPU build never tries CUDA
    monkeypatch.setattr(app, "_pipelines", {})
    monkeypatch.setattr(app, "_device", None)
    monkeypatch.setenv("FLOW_GPU", "1")
    fake["threads"] = None
    assert app.get_pipeline("a") == "cuda"
    assert fake["threads"] is None


def test_mps_when_available(fake):
    fake["mps"] = True
    assert app.get_pipeline("a") == "mps"
    assert fake["threads"] is None  # no CPU cap on the GPU


def test_cpu_capped_to_half_the_cores(fake, monkeypatch):
    fake["mps"] = False
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert app.get_pipeline("a") == "cpu"
    assert fake["threads"] == 4


def test_mps_failure_falls_back_to_cpu(fake):
    fake["mps"] = True
    fake["fail"] = {"mps"}
    assert app.get_pipeline("a") == "cpu"
    assert app._device == "cpu" and fake["threads"] >= 1
    assert app.get_pipeline("b") == "cpu"  # later languages skip the failed device
    assert fake["devices"] == ["mps", "cpu", "cpu"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="per-thread nice is Linux")
def test_background_priority_nices_the_tts_thread_only(monkeypatch):
    monkeypatch.setattr(app, "_device", "cpu")
    seen = {}

    def tts():
        with app.background_priority():
            seen["tts"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

    before = os.getpriority(os.PRIO_PROCESS, 0)
    t = threading.Thread(target=tts)
    t.start()
    t.join()
    assert seen["tts"] == max(before, 10)
    assert os.getpriority(os.PRIO_PROCESS, 0) == before  # this (Whisper-like) thread untouched
