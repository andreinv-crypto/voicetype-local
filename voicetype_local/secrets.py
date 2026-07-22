from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .paths import state_dir


_SAFE_NAME = re.compile(r"^[a-z0-9_.-]{1,64}$")
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value, max(1, len(value)))
    return (
        _DataBlob(
            len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        ),
        buffer,
    )


class WindowsDpapiProtector:
    """Protect secrets for the current Windows logon without shipping a key."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("DPAPI is available only on Windows")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._protect = self._crypt32.CryptProtectData
        self._protect.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._protect.restype = wintypes.BOOL
        self._unprotect = self._crypt32.CryptUnprotectData
        self._unprotect.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._unprotect.restype = wintypes.BOOL
        self._local_free = self._kernel32.LocalFree
        self._local_free.argtypes = (ctypes.c_void_p,)
        self._local_free.restype = ctypes.c_void_p

    def protect(self, value: bytes) -> bytes:
        source, keepalive = _blob(value)
        output = _DataBlob()
        if not self._protect(
            ctypes.byref(source),
            "VoiceType Local secret",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        del keepalive
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._local_free(output.pbData)

    def unprotect(self, value: bytes) -> bytes:
        source, keepalive = _blob(value)
        output = _DataBlob()
        description = wintypes.LPWSTR()
        if not self._unprotect(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        del keepalive
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._local_free(output.pbData)
            if description:
                self._local_free(description)


class DpapiSecretStore:
    def __init__(
        self,
        directory: Path | None = None,
        protector: SecretProtector | None = None,
    ) -> None:
        self.directory = directory or state_dir() / "secrets"
        self._protector = protector or WindowsDpapiProtector()

    def _path(self, name: str) -> Path:
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid secret name")
        return self.directory / f"{name}.dpapi"

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._protector.protect(value.encode("utf-8"))
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        temporary.replace(path)

    def set_with_rollback(self, name: str, value: str) -> Callable[[], None]:
        """Replace a secret and return a one-shot encrypted rollback action.

        The previous plaintext is never loaded.  Rollback restores the exact
        DPAPI blob that was on disk, or removes the newly-created file.
        """

        path = self._path(name)
        previous = path.read_bytes() if path.exists() else None
        self.set(name, value)
        used = False

        def rollback() -> None:
            nonlocal used
            if used:
                return
            used = True
            if previous is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(previous)
            temporary.replace(path)

        return rollback

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        return self._protector.unprotect(path.read_bytes()).decode("utf-8")

    def delete(self, name: str) -> None:
        path = self._path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def has(self, name: str) -> bool:
        return self._path(name).is_file()
