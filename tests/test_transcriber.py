import io
import wave

import voicetype_local.transcriber as transcriber_module
from voicetype_local.config import Settings
from voicetype_local.transcriber import OfflineTranscriber, normalize_transcript


def test_normalize_transcript_joins_segments_and_punctuation() -> None:
    assert normalize_transcript([" Привет ", " мир ! "]) == "Привет мир!"


def test_normalize_transcript_keeps_unicode() -> None:
    assert normalize_transcript([" España, ", "ёж и café."]) == "España, ёж и café."


def test_normalize_transcript_flattens_newlines_for_safe_chat_insertion() -> None:
    assert normalize_transcript(["Первая строка\nВторая\tстрока"]) == "Первая строка Вторая строка"


def _wav(seconds: float = 0.5, rate: int = 16_000) -> io.BytesIO:
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x00" * int(seconds * rate))
    result.seek(0)
    return result


class _FakeWorker:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.calls: list[tuple[bytes, str | None, float, str, str]] = []

    def start(self) -> None:
        self.started += 1

    def transcribe(
        self,
        audio: bytes,
        language: str | None,
        *,
        timeout: float,
        initial_prompt: str = "",
        hotwords: str = "",
    ) -> tuple[str, str, float]:
        self.calls.append((audio, language, timeout, initial_prompt, hotwords))
        return "готово", language or "ru", 0.95

    def close(self) -> None:
        self.closed += 1


def test_transcriber_keeps_parent_wav_and_uses_worker(monkeypatch, tmp_path) -> None:
    model = tmp_path / "small"
    model.mkdir()
    (model / "model.bin").write_bytes(b"test")
    monkeypatch.setattr(transcriber_module, "model_dir", lambda _: model)
    worker = _FakeWorker()

    def factory(*_args, **_kwargs):
        return worker

    transcriber = OfflineTranscriber(
        Settings(), transcription_timeout=7.0, worker_factory=factory
    )
    audio = _wav()
    original_position = audio.tell()

    assert transcriber.transcribe(
        audio,
        "auto",
        initial_prompt="VoiceType Local.",
        hotwords="GitHub",
    ) == ("готово", "ru", 0.95)
    assert audio.tell() == original_position
    assert not audio.closed
    assert worker.started == 1
    assert worker.calls[0][1:] == (None, 7.0, "VoiceType Local.", "GitHub")
    assert worker.calls[0][0].startswith(b"RIFF")

    transcriber.close()
    transcriber.close()
    assert worker.closed == 1


def test_dynamic_timeout_scales_with_wav_duration(monkeypatch, tmp_path) -> None:
    model = tmp_path / "small"
    model.mkdir()
    (model / "model.bin").write_bytes(b"test")
    monkeypatch.setattr(transcriber_module, "model_dir", lambda _: model)
    worker = _FakeWorker()
    transcriber = OfflineTranscriber(
        Settings(), worker_factory=lambda *_args, **_kwargs: worker
    )

    transcriber.transcribe(_wav(seconds=20.0), "ru")

    assert worker.calls[0][1] == "ru"
    assert worker.calls[0][2] == 55.0
    transcriber.close()
