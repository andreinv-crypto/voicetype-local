from __future__ import annotations

import math

from voicetype_local.diagnostics import TechnicalLogger


def test_diagnostics_allow_only_technical_fields(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event(
        "transcription_failed",
        duration_ms=1234,
        error_type="TimeoutError",
        transcript="секретный текст",
        dictionary="личный словарь",
        audio=b"audio",
    )
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert "transcription_failed" in content
    assert "TimeoutError" in content
    assert "секретный текст" not in content
    assert "личный словарь" not in content
    assert "audio" not in content


def test_diagnostics_rotate_and_stay_bounded(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path, max_bytes=4096, backup_count=2)
    for index in range(300):
        logger.event("heartbeat", duration_ms=index, state="processing")
    logger.close()

    files = list(tmp_path.glob("voice.log*"))
    assert 1 <= len(files) <= 3
    assert sum(item.stat().st_size for item in files) < 20_000


def test_diagnostics_reject_transcript_smuggled_as_event_name(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event("секретный текст пользователя", duration_ms=12)
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert "unknown_event" in content
    assert "секретный текст пользователя" not in content


def test_voice_command_logs_accept_metadata_but_never_spoken_text(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event(
        "voice_command_completed",
        operation="scroll",
        code="ok",
        backend="uia",
        transcript="секретная голосовая команда",
        target_name="Личный документ",
    )
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert "voice_command_completed" in content
    assert '"operation":"scroll"' in content
    assert '"backend":"uia"' in content
    assert "секретная голосовая команда" not in content
    assert "Личный документ" not in content


def test_text_insertion_failure_keeps_its_allowlisted_event_name(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event(
        "text_insertion_failed",
        operation="richedit_targeted",
        code="targeted_insert_timeout",
        transcript="секретный текст",
    )
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert '"event":"text_insertion_failed"' in content
    assert "targeted_insert_timeout" in content
    assert "секретный текст" not in content


def test_dictation_transform_logs_only_bounded_metadata(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event(
        "dictation_transform_completed",
        operation="commands",
        code="comma+new_line",
        count=2,
        transcript="секретный исходный текст",
        transformed_text="секретный исправленный текст",
    )
    logger.event(
        "dictation_transform_completed",
        operation="fillers",
        code="closed_list",
        count=100_001,
    )
    logger.event(
        "dictation_transform_completed",
        operation="commands",
        code="comma",
        count="2",
    )
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert '"event":"dictation_transform_completed"' in content
    assert '"code":"comma+new_line"' in content
    assert '"count":2' in content
    assert '"count":10000' in content
    assert '"count":"2"' not in content
    assert "секретный исходный текст" not in content
    assert "секретный исправленный текст" not in content


def test_allowlisted_fields_still_fail_closed_by_type_and_shape(tmp_path) -> None:
    path = tmp_path / "voice.log"
    logger = TechnicalLogger(path)
    logger.event(
        "heartbeat",
        operation="секретный текст",
        code="line one\nline two",
        backend={"private": "value"},
        state="processing",
        duration_ms=math.inf,
        retry_count="3",
        worker_pid=9_999_999_999,
    )
    logger.close()

    content = path.read_text(encoding="utf-8")
    assert '"state":"processing"' in content
    assert '"worker_pid":2147483647' in content
    assert "секретный текст" not in content
    assert "line one" not in content
    assert "private" not in content
    assert "duration_ms" not in content
    assert "retry_count" not in content
