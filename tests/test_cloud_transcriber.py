from __future__ import annotations

import io

import pytest

from voicetype_local.cloud_transcriber import OpenAICloudTranscriber


class _Transport:
    def __init__(self) -> None:
        self.body = b""
        self.headers = {}

    def post(self, url, body, headers, timeout):
        assert url.endswith("/audio/transcriptions")
        assert timeout == 30.0
        self.body = body
        self.headers = dict(headers)
        return {"text": "Готово", "language": "ru"}


def test_cloud_transcription_requires_separate_consent() -> None:
    with pytest.raises(PermissionError):
        OpenAICloudTranscriber(
            api_key="test", model="configured-model", consent=False
        )


def test_cloud_transcription_rejects_non_official_endpoint() -> None:
    with pytest.raises(ValueError):
        OpenAICloudTranscriber(
            api_key="test",
            model="configured-model",
            consent=True,
            endpoint="http://example.test/audio/transcriptions",
        )


def test_cloud_transcriber_sends_in_memory_audio_only_when_enabled() -> None:
    transport = _Transport()
    provider = OpenAICloudTranscriber(
        api_key="test",
        model="configured-model",
        consent=True,
        timeout=30,
        transport=transport,
    )
    audio = io.BytesIO(b"RIFF-private-audio")
    result = provider.transcribe(audio, "ru", "VoiceType Local")

    assert result == ("Готово", "ru", -1.0)
    assert b"RIFF-private-audio" in transport.body
    assert b'filename="dictation.wav"' in transport.body
    assert b"VoiceType Local" in transport.body
    assert transport.headers["Authorization"] == "Bearer test"
    assert audio.tell() == 0
