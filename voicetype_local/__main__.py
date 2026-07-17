import ctypes
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

from voicetype_local.app import run_app, run_ui_smoke


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _single_instance() -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\VoiceTypeLocal-4A5B2A94")
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        _close_handle(int(handle))
        return 0
    return int(handle)


def _self_test() -> int:
    """Exercise packaged native dependencies without opening the UI or microphone."""
    from voicetype_local.packaged_self_test import run_packaged_control_self_test

    control_code, _control_label = run_packaged_control_self_test()
    if control_code:
        return control_code

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


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if "--ui-smoke-test" in sys.argv:
        run_ui_smoke()
        raise SystemExit(0)
    mutex = _single_instance()
    if mutex == 0:
        raise SystemExit(0)
    try:
        run_app()
    finally:
        if mutex:
            _close_handle(mutex)
