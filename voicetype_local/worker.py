from __future__ import annotations

import io
import multiprocessing as mp
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from .worker_protocol import (
    MSG_CLOSE,
    MSG_CLOSED,
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_TRANSCRIBE,
    is_message,
    message,
    normalize_transcript,
    safe_error_text,
)


class WorkerError(RuntimeError):
    pass


class WorkerTimeoutError(WorkerError):
    pass


class WorkerClosedError(WorkerError):
    pass


def _create_model(config: dict[str, Any]) -> Any:
    # Importing CTranslate2 only in the child keeps native inference and its
    # memory outside the GUI process. A native crash can then be recovered by
    # replacing this process.
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(config["model_path"]),
        device="cpu",
        compute_type=str(config["compute_type"]),
        cpu_threads=int(config["cpu_threads"]),
        local_files_only=True,
    )


def _transcribe(
    model: Any,
    audio: bytes,
    language: str | None,
    initial_prompt: str = "",
    hotwords: str = "",
) -> tuple[str, str, float]:
    segments, info = model.transcribe(
        io.BytesIO(audio),
        language=language,
        task="transcribe",
        beam_size=1,
        best_of=1,
        temperature=0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,
        initial_prompt=initial_prompt or None,
        hotwords=hotwords or None,
    )
    text = normalize_transcript([segment.text for segment in segments])
    return text, str(info.language), float(info.language_probability)


def _worker_loop(connection: Connection, model: Any) -> None:
    while True:
        try:
            request = connection.recv()
        except (EOFError, OSError):
            return

        if is_message(request, MSG_CLOSE):
            try:
                connection.send(message(MSG_CLOSED))
            except (BrokenPipeError, EOFError, OSError):
                pass
            return

        if not is_message(request, MSG_TRANSCRIBE):
            try:
                connection.send(
                    message(
                        MSG_ERROR,
                        request_id=(request.get("request_id") if isinstance(request, dict) else None),
                        stage="protocol",
                        error_type="ProtocolError",
                        error="Unsupported worker request",
                    )
                )
            except (BrokenPipeError, EOFError, OSError):
                return
            continue

        request_id = request.get("request_id")
        audio = request.get("audio")
        language = request.get("language")
        initial_prompt = request.get("initial_prompt", "")
        hotwords = request.get("hotwords", "")
        if not isinstance(request_id, int) or not isinstance(audio, bytes):
            try:
                connection.send(
                    message(
                        MSG_ERROR,
                        request_id=request_id,
                        stage="protocol",
                        error_type="ProtocolError",
                        error="Invalid transcription request",
                    )
                )
            except (BrokenPipeError, EOFError, OSError):
                return
            continue

        try:
            text, detected_language, probability = _transcribe(
                model,
                audio,
                language if isinstance(language, str) else None,
                initial_prompt[:4000] if isinstance(initial_prompt, str) else "",
                hotwords[:4000] if isinstance(hotwords, str) else "",
            )
            response = message(
                MSG_RESULT,
                request_id=request_id,
                text=text,
                language=detected_language,
                probability=probability,
            )
        except Exception as exc:
            response = message(
                MSG_ERROR,
                request_id=request_id,
                stage="transcription",
                error_type=exc.__class__.__name__,
                error=safe_error_text(exc),
            )
        try:
            connection.send(response)
        except (BrokenPipeError, EOFError, OSError):
            return


def whisper_worker_main(connection: Connection, config: dict[str, Any]) -> None:
    """Top-level spawn target required by Windows and frozen PyInstaller apps."""
    try:
        try:
            model = _create_model(config)
        except Exception as exc:
            try:
                connection.send(
                    message(
                        MSG_ERROR,
                        stage="startup",
                        error_type=exc.__class__.__name__,
                        error=safe_error_text(exc),
                    )
                )
            except (BrokenPipeError, EOFError, OSError):
                pass
            return
        try:
            connection.send(message(MSG_READY))
        except (BrokenPipeError, EOFError, OSError):
            return
        _worker_loop(connection, model)
    finally:
        try:
            connection.close()
        except OSError:
            pass


class WhisperWorkerClient:
    """One-at-a-time client for a replaceable persistent Whisper process."""

    def __init__(
        self,
        model_path: Path,
        compute_type: str,
        cpu_threads: int,
        *,
        startup_timeout: float = 90.0,
        shutdown_timeout: float = 2.0,
        context_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config = {
            "model_path": str(model_path),
            "compute_type": compute_type,
            "cpu_threads": cpu_threads,
        }
        self.startup_timeout = max(0.1, float(startup_timeout))
        self.shutdown_timeout = max(0.1, float(shutdown_timeout))
        self._context_factory = context_factory or (lambda: mp.get_context("spawn"))
        self._operation_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._process: Any | None = None
        self._connection: Connection | None = None
        self._closed = threading.Event()
        self._request_id = 0

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return bool(self._process is not None and self._process.is_alive())

    def _check_open(self) -> None:
        if self._closed.is_set():
            raise WorkerClosedError("Whisper worker is closed")

    @staticmethod
    def _format_remote_error(response: dict[str, Any]) -> WorkerError:
        error_type = str(response.get("error_type", "WorkerError"))
        detail = str(response.get("error", "Unknown worker error"))
        stage = str(response.get("stage", "worker"))
        return WorkerError(f"Whisper {stage} failed ({error_type}): {detail}")

    def _wait_message(self, connection: Connection, timeout: float, stage: str) -> Any:
        try:
            if not connection.poll(timeout):
                raise WorkerTimeoutError(f"Whisper {stage} timed out after {timeout:.1f}s")
            return connection.recv()
        except WorkerError:
            raise
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise WorkerError(f"Whisper worker disconnected during {stage}") from exc

    @staticmethod
    def _send_message(
        connection: Connection,
        payload: dict[str, Any],
        timeout: float,
        stage: str,
    ) -> None:
        completed = threading.Event()
        errors: list[BaseException] = []

        def send() -> None:
            try:
                connection.send(payload)
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        threading.Thread(
            target=send,
            name=f"whisper-pipe-{stage}",
            daemon=True,
        ).start()
        if not completed.wait(timeout):
            raise WorkerTimeoutError(f"Whisper {stage} timed out after {timeout:.1f}s")
        if errors:
            raise WorkerError(f"Whisper worker disconnected during {stage}") from errors[0]

    def _spawn_once(self) -> None:
        self._check_open()
        deadline = time.monotonic() + self.startup_timeout
        context = self._context_factory()
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=whisper_worker_main,
            args=(child_connection, self._config),
            name="VoiceType-Whisper",
            daemon=True,
        )
        start_completed = threading.Event()
        start_cancelled = threading.Event()
        start_errors: list[BaseException] = []

        def start_process() -> None:
            try:
                process.start()
            except BaseException as exc:
                start_errors.append(exc)
            finally:
                # The spawned child owns its duplicate. Keeping this endpoint
                # in the parent would prevent EOF detection after a crash.
                try:
                    child_connection.close()
                except OSError:
                    pass
                if start_cancelled.is_set() and not start_errors:
                    self._terminate_process(process, parent_connection)
                start_completed.set()

        threading.Thread(
            target=start_process,
            name="whisper-process-start",
            daemon=True,
        ).start()
        if not start_completed.wait(self.startup_timeout):
            start_cancelled.set()
            try:
                parent_connection.close()
            except OSError:
                pass
            raise WorkerTimeoutError(
                f"Whisper startup timed out after {self.startup_timeout:.1f}s"
            )
        if start_errors:
            try:
                parent_connection.close()
            except OSError:
                pass
            error = start_errors[0]
            raise WorkerError(f"Could not create Whisper worker: {error}") from error

        with self._lifecycle_lock:
            if self._closed.is_set():
                self._terminate_process(process, parent_connection)
                raise WorkerClosedError("Whisper worker is closed")
            self._process = process
            self._connection = parent_connection

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerTimeoutError(
                f"Whisper startup timed out after {self.startup_timeout:.1f}s"
            )
        response = self._wait_message(parent_connection, remaining, "startup")
        if is_message(response, MSG_READY):
            return
        if is_message(response, MSG_ERROR):
            raise self._format_remote_error(response)
        raise WorkerError("Whisper worker returned an invalid startup response")

    def start(self) -> None:
        with self._operation_lock:
            self._check_open()
            if self.is_alive:
                return
            last_error: Exception | None = None
            for _ in range(2):
                self._stop_current(graceful=False)
                try:
                    self._spawn_once()
                    return
                except WorkerClosedError:
                    raise
                except Exception as exc:
                    last_error = exc
                    self._stop_current(graceful=False)
            assert last_error is not None
            if isinstance(last_error, WorkerError):
                raise last_error
            raise WorkerError(f"Could not start Whisper worker: {last_error}") from last_error

    def _ensure_started_once(self) -> tuple[Any, Connection]:
        self._check_open()
        with self._lifecycle_lock:
            process = self._process
            connection = self._connection
        if process is None or connection is None or not process.is_alive():
            self._stop_current(graceful=False)
            self._spawn_once()
            with self._lifecycle_lock:
                process = self._process
                connection = self._connection
        if process is None or connection is None:
            raise WorkerError("Whisper worker did not start")
        return process, connection

    def transcribe(
        self,
        audio: bytes,
        language: str | None,
        *,
        timeout: float,
        initial_prompt: str = "",
        hotwords: str = "",
    ) -> tuple[str, str, float]:
        if not isinstance(audio, bytes) or not audio:
            raise ValueError("Audio buffer is empty")
        timeout = max(0.1, float(timeout))
        with self._operation_lock:
            self._check_open()
            last_error: Exception | None = None
            for _ in range(2):
                self._check_open()
                try:
                    _, connection = self._ensure_started_once()
                    self._request_id += 1
                    request_id = self._request_id
                    deadline = time.monotonic() + timeout
                    self._send_message(
                        connection,
                        message(
                            MSG_TRANSCRIBE,
                            request_id=request_id,
                            audio=audio,
                            language=language,
                            initial_prompt=str(initial_prompt)[:4000],
                            hotwords=str(hotwords)[:4000],
                        ),
                        timeout,
                        "request transfer",
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerTimeoutError(
                            f"Whisper transcription timed out after {timeout:.1f}s"
                        )
                    response = self._wait_message(connection, remaining, "transcription")
                    if is_message(response, MSG_RESULT) and response.get("request_id") == request_id:
                        return (
                            str(response.get("text", "")),
                            str(response.get("language", "")),
                            float(response.get("probability", -1.0)),
                        )
                    if is_message(response, MSG_ERROR) and response.get("request_id") == request_id:
                        raise self._format_remote_error(response)
                    raise WorkerError("Whisper worker returned an invalid transcription response")
                except WorkerClosedError:
                    raise
                except Exception as exc:
                    last_error = exc
                    self._stop_current(graceful=False)
            assert last_error is not None
            if isinstance(last_error, WorkerError):
                raise last_error
            raise WorkerError(f"Whisper transcription failed: {last_error}") from last_error

    @staticmethod
    def _terminate_process(process: Any | None, connection: Connection | None) -> None:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2.0)
            if process.is_alive():
                kill = getattr(process, "kill", None)
                if kill is not None:
                    kill()
                process.join(timeout=1.0)
        except (AssertionError, OSError, ValueError):
            pass
        try:
            process.close()
        except (AttributeError, OSError, ValueError):
            pass

    def _stop_current(self, *, graceful: bool) -> None:
        with self._lifecycle_lock:
            process = self._process
            connection = self._connection
            self._process = None
            self._connection = None
        if process is None and connection is None:
            return

        if graceful and process is not None and connection is not None and process.is_alive():
            try:
                connection.send(message(MSG_CLOSE))
                if connection.poll(self.shutdown_timeout):
                    response = connection.recv()
                    if is_message(response, MSG_CLOSED):
                        process.join(timeout=self.shutdown_timeout)
            except (BrokenPipeError, EOFError, OSError):
                pass
        self._terminate_process(process, connection)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        # close() must remain able to kill an in-flight native inference; it
        # intentionally does not wait for _operation_lock.
        self._stop_current(graceful=False)

    def __enter__(self) -> WhisperWorkerClient:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
