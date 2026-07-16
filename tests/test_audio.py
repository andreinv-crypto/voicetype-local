import io
import threading
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


class _FailingStopStream(_FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.aborted = False

    def stop(self) -> None:
        raise OSError("driver stop failed")

    def abort(self) -> None:
        self.aborted = True


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


def test_stop_salvages_recorded_audio_when_driver_stop_fails() -> None:
    recorder = AudioRecorder()
    stream = _FailingStopStream()
    recorder._stream = stream  # type: ignore[assignment]
    recorder._sample_rate = 16_000
    recorder._frames = [b"\x00\x00" * 160]

    result = recorder.stop()

    with wave.open(result, "rb") as source:
        assert source.getnframes() == 160
    assert stream.aborted
    assert stream.closed


def test_start_falls_back_to_windows_default(monkeypatch) -> None:
    opened: list[tuple[object, _FakeStream]] = []

    class _OpenStream(_FakeStream):
        def __init__(self, device: object) -> None:
            super().__init__()
            self.device = device

        def start(self) -> None:
            if self.device == 7:
                raise OSError("WASAPI busy")

        def abort(self) -> None:
            pass

    def raw_input_stream(**kwargs):
        stream = _OpenStream(kwargs["device"])
        opened.append((kwargs["device"], stream))
        return stream

    monkeypatch.setattr(AudioRecorder, "_resolve_device", classmethod(lambda cls, _: 7))
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda device, kind: {"default_samplerate": 48_000.0},
    )
    monkeypatch.setattr(audio_module.sd, "RawInputStream", raw_input_stream)
    recorder = AudioRecorder()

    recorder.start("default")

    assert [device for device, _ in opened] == [7, None]
    assert opened[0][1].closed
    assert recorder.is_recording
    recorder.cancel()


def test_cancel_aborts_stream_blocked_in_start(monkeypatch) -> None:
    entered = threading.Event()
    aborted = threading.Event()
    errors: list[Exception] = []

    class _BlockingStartStream(_FakeStream):
        def start(self) -> None:
            entered.set()
            aborted.wait(2.0)

        def abort(self) -> None:
            aborted.set()

    stream = _BlockingStartStream()
    monkeypatch.setattr(AudioRecorder, "_resolve_device", classmethod(lambda cls, _: 1))
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda device, kind: {"default_samplerate": 16_000.0},
    )
    monkeypatch.setattr(audio_module.sd, "RawInputStream", lambda **_: stream)
    recorder = AudioRecorder()

    def run_start() -> None:
        try:
            recorder.start("default")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_start)
    thread.start()
    assert entered.wait(0.5)
    recorder.cancel()
    thread.join(0.5)

    assert not thread.is_alive()
    assert aborted.is_set()
    assert stream.closed
    assert errors
    assert not recorder.is_recording


def test_cancel_aborts_stream_blocked_in_stop() -> None:
    entered = threading.Event()
    aborted = threading.Event()
    errors: list[Exception] = []

    class _BlockingStopStream(_FakeStream):
        def stop(self) -> None:
            entered.set()
            aborted.wait(2.0)

        def abort(self) -> None:
            aborted.set()

    recorder = AudioRecorder()
    stream = _BlockingStopStream()
    recorder._stream = stream  # type: ignore[assignment]
    recorder._frames = [b"\x00\x00" * 160]

    def run_stop() -> None:
        try:
            recorder.stop()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_stop)
    thread.start()
    assert entered.wait(0.5)
    recorder.cancel()
    thread.join(0.5)

    assert not thread.is_alive()
    assert aborted.is_set()
    assert not recorder.is_recording


def test_native_constructor_without_handle_is_exposed_to_circuit_breaker(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    class _OpenedStream(_FakeStream):
        def start(self) -> None:
            return

        def abort(self) -> None:
            return

    def blocking_constructor(**_kwargs):
        entered.set()
        release.wait(2.0)
        return _OpenedStream()

    monkeypatch.setattr(AudioRecorder, "_resolve_device", classmethod(lambda cls, _: 1))
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda device, kind: {"default_samplerate": 16_000.0},
    )
    monkeypatch.setattr(audio_module.sd, "RawInputStream", blocking_constructor)
    recorder = AudioRecorder()

    def run_start() -> None:
        try:
            recorder.start("default")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_start)
    thread.start()
    assert entered.wait(0.5)
    assert recorder.native_open_unabortable is True
    recorder.cancel()
    release.set()
    thread.join(0.5)

    assert not thread.is_alive()
    assert recorder.native_open_unabortable is False
    assert errors
