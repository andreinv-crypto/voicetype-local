import ctypes
import os
import sys

from voicetype_local.app import run_app


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
    from voicetype_local.audio import AudioRecorder
    from voicetype_local.config import Settings
    from voicetype_local.inserter import INPUT, UnicodeTextInserter
    from voicetype_local.transcriber import OfflineTranscriber

    if ctypes.sizeof(INPUT) != 40:
        return 11
    if not AudioRecorder.input_devices():
        return 12
    UnicodeTextInserter()
    OfflineTranscriber(Settings()).load()
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if "--ui-smoke-test" in sys.argv:
        run_app(3000)
        raise SystemExit(0)
    mutex = _single_instance()
    if mutex == 0:
        raise SystemExit(0)
    try:
        run_app()
    finally:
        if mutex:
            _close_handle(mutex)
