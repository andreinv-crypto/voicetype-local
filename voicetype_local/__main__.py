import ctypes
import multiprocessing
import sys
import tempfile
from pathlib import Path

from voicetype_local.app import run_app, run_ui_smoke
from voicetype_local.instance_activation import InstanceActivation


def _self_test() -> int:
    """Exercise packaged native dependencies without opening the UI or microphone."""
    from voicetype_local.packaged_self_test import run_packaged_control_self_test

    control_code, _control_label = run_packaged_control_self_test()
    if control_code:
        return control_code

    from voicetype_local.windows_session_editor_probe import (
        run_isolated_session_editor_probe,
    )

    editor_code, _editor_label = run_isolated_session_editor_probe()
    if editor_code:
        return editor_code

    from voicetype_local.audio import AudioRecorder
    from voicetype_local.config import Settings
    from voicetype_local.inserter import INPUT, UnicodeTextInserter
    from voicetype_local.focus import FocusInspector
    from voicetype_local.memory import MemoryStore
    from voicetype_local.prompt_builder import PromptBuilder
    from voicetype_local.transcriber import OfflineTranscriber

    if ctypes.sizeof(INPUT) != 40:
        return 11
    if not AudioRecorder.input_devices():
        return 12
    UnicodeTextInserter()
    FocusInspector().capture()
    with tempfile.TemporaryDirectory(prefix="voicetype-self-test-") as directory:
        memory = MemoryStore(Path(directory) / "memory.sqlite3", enable_fts=True)
        try:
            if not memory.integrity_check():
                return 13
            PromptBuilder().build_pre_asr(memory)
        finally:
            memory.close()
    transcriber = OfflineTranscriber(Settings())
    try:
        transcriber.load()
    finally:
        transcriber.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in arguments:
        return _self_test()
    if "--ui-smoke-test" in arguments:
        run_ui_smoke()
        return 0

    background = "--background" in arguments
    activation = InstanceActivation.acquire()
    if not activation.is_primary:
        try:
            # Startup is deliberately silent. A normal Desktop launch is a
            # user request to surface the already-running application's UI.
            if not background:
                activation.notify_settings()
        finally:
            activation.close()
        return 0

    try:
        run_app(
            activation_waiter=activation.wait_for_settings,
            open_settings_on_start=not background,
            silent_start=background,
        )
    finally:
        activation.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
