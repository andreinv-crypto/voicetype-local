from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from typing import Any, BinaryIO, Protocol


OPENAI_TRANSCRIPTIONS_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"


class MultipartTransport(Protocol):
    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]: ...


class UrllibMultipartTransport:
    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Cloud transcription provider is unavailable") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("Cloud transcription response is too large")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("Cloud transcription response is not an object")
        return parsed


def _multipart(
    fields: Mapping[str, str], file_bytes: bytes, boundary: str
) -> bytes:
    output = io.BytesIO()
    for name, value in fields.items():
        output.write(f"--{boundary}\r\n".encode("ascii"))
        output.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
        )
        output.write(value.encode("utf-8"))
        output.write(b"\r\n")
    output.write(f"--{boundary}\r\n".encode("ascii"))
    output.write(
        b'Content-Disposition: form-data; name="file"; filename="dictation.wav"\r\n'
    )
    output.write(b"Content-Type: audio/wav\r\n\r\n")
    output.write(file_bytes)
    output.write(b"\r\n")
    output.write(f"--{boundary}--\r\n".encode("ascii"))
    return output.getvalue()


class OpenAICloudTranscriber:
    """Explicit-consent cloud mode. The local transcriber remains the fallback."""

    name = "cloud_transcription:openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        consent: bool,
        timeout: float = 90.0,
        transport: MultipartTransport | None = None,
        endpoint: str = OPENAI_TRANSCRIPTIONS_ENDPOINT,
    ) -> None:
        if not consent:
            raise PermissionError("Cloud transcription requires separate consent")
        if not api_key.strip():
            raise ValueError("OpenAI API key is not configured")
        if not model.strip():
            raise ValueError("Cloud transcription model is not configured")
        if endpoint != OPENAI_TRANSCRIPTIONS_ENDPOINT:
            raise ValueError("OpenAI transcription endpoint is fixed to official HTTPS")
        self._api_key = api_key
        self._model = model.strip()
        self._timeout = min(10 * 60.0, max(10.0, float(timeout)))
        self._transport = transport or UrllibMultipartTransport()
        self._endpoint = endpoint

    def transcribe(
        self,
        audio: BinaryIO,
        language: str = "auto",
        prompt: str = "",
    ) -> tuple[str, str, float]:
        audio.seek(0)
        file_bytes = audio.read(24 * 1024 * 1024 + 1)
        audio.seek(0)
        if len(file_bytes) > 24 * 1024 * 1024:
            raise RuntimeError("Audio is too large for configured cloud transcription")
        boundary = f"VoiceType-{uuid.uuid4().hex}"
        fields = {"model": self._model, "response_format": "json"}
        if language and language != "auto":
            fields["language"] = language
        if prompt.strip():
            fields["prompt"] = prompt.strip()[:4000]
        response = self._transport.post(
            self._endpoint,
            _multipart(fields, file_bytes, boundary),
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            self._timeout,
        )
        text = response.get("text")
        if not isinstance(text, str):
            raise RuntimeError("Cloud transcription returned no text")
        detected = response.get("language")
        if not isinstance(detected, str):
            detected = language if language != "auto" else "unknown"
        return text.strip(), detected, -1.0
