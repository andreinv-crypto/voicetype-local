import io
import wave
from types import SimpleNamespace

import voicetype_local.audio as audio_module
from voicetype_local.audio import AudioRecorder


class _FakeStream:
    def __init__(self) -> None:
        self.stopped = False
        self.closed = False

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def test_stop_builds_an_in_memory_wav() -> None:
    recorder = AudioRecorder()
    stream = _FakeStream()
    recorder._stream = stream  # type: ignore[assignment]
    recorder._sample_rate = 16_000
    recorder._frames = [b"\x00\x00" * 160]

    result = recorder.stop()

    assert isinstance(result, io.BytesIO)
    with wave.open(result, "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == 16_000
        assert source.getnframes() == 160
    assert stream.stopped
    assert stream.closed


def test_default_microphone_prefers_matching_wasapi(monkeypatch) -> None:
    devices = [
        {
            "name": "Micrófono (USB PnP Audio Device",
            "max_input_channels": 1,
            "hostapi": 0,
        },
        {
            "name": "Micrófono (USB PnP Audio Device)",
            "max_input_channels": 1,
            "hostapi": 1,
        },
    ]
    host_apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
    monkeypatch.setattr(audio_module.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(audio_module.sd, "query_hostapis", lambda: host_apis)
    monkeypatch.setattr(audio_module.sd, "default", SimpleNamespace(device=(0, -1)))

    assert AudioRecorder._resolve_device("default") == 1
