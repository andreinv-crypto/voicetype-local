from __future__ import annotations

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
