from __future__ import annotations

from collections import deque
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from voicetype_local.worker import (
    WhisperWorkerClient,
    WorkerTimeoutError,
    _worker_loop,
)
from voicetype_local.worker_protocol import (
    MSG_CLOSE,
    MSG_CLOSED,
    MSG_READY,
    MSG_RESULT,
    MSG_TRANSCRIBE,
    is_message,
    message,
)


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class _Model:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def transcribe(self, audio, **kwargs):
        assert audio.read() == b"wav"
        self.kwargs = kwargs
        return iter([_Segment(" Привет "), _Segment(" мир !")]), SimpleNamespace(
            language="ru", language_probability=0.9
        )


class _LoopConnection:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict[str, Any]] = []

    def recv(self):
        if not self.incoming:
            raise EOFError
        return self.incoming.popleft()

    def send(self, value):
        self.sent.append(value)


def test_worker_loop_transcribes_and_closes() -> None:
    model = _Model()
    connection = _LoopConnection(
        [
            message(
                MSG_TRANSCRIBE,
                request_id=1,
                audio=b"wav",
                language="ru",
                initial_prompt="VoiceType Local.",
                hotwords="GitHub, WordPress",
            ),
            message(MSG_CLOSE),
        ]
    )

    _worker_loop(connection, model)  # type: ignore[arg-type]

    assert is_message(connection.sent[0], MSG_RESULT)
    assert connection.sent[0]["text"] == "Привет мир!"
    assert connection.sent[0]["language"] == "ru"
    assert connection.sent[0]["probability"] == 0.9
    assert model.kwargs["beam_size"] == 1
    assert model.kwargs["best_of"] == 1
    assert model.kwargs["initial_prompt"] == "VoiceType Local."
    assert model.kwargs["hotwords"] == "GitHub, WordPress"
    assert is_message(connection.sent[1], MSG_CLOSED)


_TIMEOUT = object()


class _FakeConnection:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = deque(responses or [])
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def poll(self, _timeout: float) -> bool:
        return bool(self.responses and self.responses[0] is not _TIMEOUT)

    def recv(self):
        return self.responses.popleft()

    def send(self, value):
        self.sent.append(value)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = False
        self.terminated = False
        self.closed = False

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.terminated = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, responses: list[Any]) -> None:
        self.parent = _FakeConnection(responses)
        self.child = _FakeConnection()
        self.process = _FakeProcess()

    def Pipe(self, duplex: bool = True):
        assert duplex
        return self.parent, self.child

    def Process(self, **_kwargs):
        return self.process


class _BlockingConnection(_FakeConnection):
    def __init__(self) -> None:
        super().__init__([message(MSG_READY)])
        self.released = threading.Event()

    def send(self, value):
        if is_message(value, MSG_TRANSCRIBE):
            self.released.wait()
        else:
            super().send(value)

    def close(self) -> None:
        self.released.set()
        super().close()


class _BlockingContext(_FakeContext):
    def __init__(self) -> None:
        super().__init__([])
        self.parent = _BlockingConnection()


def test_client_times_out_kills_and_retries_once(tmp_path) -> None:
    first = _FakeContext([message(MSG_READY), _TIMEOUT])
    second = _FakeContext(
        [
            message(MSG_READY),
            message(
                MSG_RESULT,
                request_id=2,
                text="готово",
                language="ru",
                probability=0.8,
            ),
        ]
    )
    contexts = iter([first, second])
    client = WhisperWorkerClient(
        tmp_path,
        "int8",
        4,
        startup_timeout=0.1,
        context_factory=lambda: next(contexts),
    )

    assert client.transcribe(b"wav", "ru", timeout=0.1) == (
        "готово",
        "ru",
        0.8,
    )
    assert first.process.terminated
    assert len(first.parent.sent) == 1
    assert len(second.parent.sent) == 1
    assert second.parent.sent[0]["request_id"] == 2

    client.close()
    assert second.process.terminated
    assert not client.is_alive


def test_request_transfer_is_bounded_and_retried_once(tmp_path) -> None:
    first = _BlockingContext()
    second = _BlockingContext()
    contexts = iter([first, second])
    client = WhisperWorkerClient(
        tmp_path,
        "int8",
        4,
        startup_timeout=0.1,
        context_factory=lambda: next(contexts),
    )

    with pytest.raises(WorkerTimeoutError, match="request transfer timed out"):
        client.transcribe(b"wav", "ru", timeout=0.1)

    assert first.process.terminated
    assert second.process.terminated
    client.close()
