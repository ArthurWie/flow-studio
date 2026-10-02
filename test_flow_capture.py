"""start_capture() falls back to the default mic when the saved one fails. Fakes sounddevice."""
import sys
import types

import flow


def test_stale_mic_falls_back_to_default(monkeypatch):
    opened = []

    class FakeStream:
        def __init__(self, device=None, **kw):
            if device is not None:
                raise ValueError("Error querying device 7")
            opened.append(device)

        def start(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=FakeStream))
    monkeypatch.setattr(flow, "settings", {**flow.settings, "mic_index": 7})
    flow.start_capture()
    assert opened == [None]
    assert isinstance(flow._stream, FakeStream)
    flow._stream = None
