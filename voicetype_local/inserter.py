from __future__ import annotations

import ctypes
import time
import unicodedata
from collections.abc import Callable
from ctypes import wintypes


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_BACK = 0x08

ULONG_PTR = wintypes.WPARAM


class TextInsertionError(RuntimeError):
    """SendInput failure with an explicit partial-insertion safety signal."""

    def __init__(
        self,
        message: str,
        *,
        partial: bool,
        reason_code: str = "sendinput_failed",
    ) -> None:
        super().__init__(message)
        self.partial = partial
        self.reason_code = reason_code


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def _unicode_units(character: str) -> list[int]:
    encoded = character.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]


class UnicodeTextInserter:
    """Type Unicode through SendInput without changing the user's clipboard."""

    def __init__(self, chunk_size: int = 192) -> None:
        self.chunk_size = chunk_size
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send_input = self._user32.SendInput
        self._send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._send_input.restype = wintypes.UINT

    @staticmethod
    def _check_batch_guard(before_batch: Callable[[], bool] | None) -> None:
        if before_batch is None:
            return
        try:
            allowed = bool(before_batch())
        except Exception:
            allowed = False
        if not allowed:
            raise TextInsertionError(
                "Input target changed before SendInput",
                partial=False,
                reason_code="input_guard_rejected",
            )

    def _send(
        self,
        events: list[INPUT],
        *,
        before_batch: Callable[[], bool] | None = None,
    ) -> None:
        if not events:
            return
        array_type = INPUT * len(events)
        array = array_type(*events)
        # This is deliberately adjacent to the native call: every SendInput
        # batch gets a fresh target/focus proof from the session editor.
        self._check_batch_guard(before_batch)
        sent = self._send_input(len(events), array, ctypes.sizeof(INPUT))
        if sent != len(events):
            error = ctypes.WinError(ctypes.get_last_error())
            raise TextInsertionError(
                f"Windows inserted {sent} of {len(events)} keyboard events: {error}",
                partial=sent > 0,
            ) from error

    @staticmethod
    def _validated_text(text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        # Preserve dictated layout as Unicode text. CRLF is normalized first so
        # Windows line endings never become two line breaks; neither newline nor
        # tab is emitted as a physical VK_RETURN/VK_TAB key event.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if any(
            character not in {"\n", "\t"}
            and not character.isprintable()
            and unicodedata.category(character) != "Cf"
            for character in normalized
        ):
            raise TextInsertionError(
                "Text contains an unsupported control or surrogate character",
                partial=False,
            )
        return normalized

    def insert(
        self,
        text: str,
        *,
        before_batch: Callable[[], bool] | None = None,
    ) -> None:
        normalized = self._validated_text(text)
        events: list[INPUT] = []
        prior_batch_sent = False
        for character in normalized:
            for unit in _unicode_units(character):
                events.extend(
                    [
                        _key_event(0, unit, KEYEVENTF_UNICODE),
                        _key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                    ]
                )
            if len(events) >= self.chunk_size * 2:
                try:
                    if before_batch is None:
                        self._send(events)
                    else:
                        self._send(events, before_batch=before_batch)
                except TextInsertionError as exc:
                    if prior_batch_sent and not exc.partial:
                        raise TextInsertionError(
                            str(exc),
                            partial=True,
                            reason_code=exc.reason_code,
                        ) from exc
                    raise
                prior_batch_sent = True
                events.clear()
                time.sleep(0.002)
        try:
            if before_batch is None:
                self._send(events)
            else:
                self._send(events, before_batch=before_batch)
        except TextInsertionError as exc:
            if prior_batch_sent and not exc.partial:
                raise TextInsertionError(
                    str(exc),
                    partial=True,
                    reason_code=exc.reason_code,
                ) from exc
            raise

    def insert_atomic(
        self,
        text: str,
        *,
        before_batch: Callable[[], bool] | None = None,
    ) -> None:
        """Insert all Unicode keyboard events with one ``SendInput`` call.

        This path is intentionally separate from :meth:`insert`: callers that
        need the legacy bounded batches keep their existing behaviour.  The
        complete event array is built only after text validation, then
        ``_send`` evaluates the single guard immediately before crossing the
        native boundary.
        """

        normalized = self._validated_text(text)
        events: list[INPUT] = []
        for character in normalized:
            for unit in _unicode_units(character):
                events.extend(
                    [
                        _key_event(0, unit, KEYEVENTF_UNICODE),
                        _key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                    ]
                )
        self._send(events, before_batch=before_batch)

    def replace_selection(self, text: str) -> None:
        """Replace an already verified selection without using the clipboard.

        Non-empty Unicode input naturally replaces the active selection.  An
        empty replacement uses one Backspace key event, which deletes only the
        selection prepared and re-checked by the session editor.
        """

        normalized = self._validated_text(text)
        if normalized:
            self.insert(normalized)
            return
        self._send(
            [
                _key_event(VK_BACK, 0, 0),
                _key_event(VK_BACK, 0, KEYEVENTF_KEYUP),
            ]
        )

    def replace_selection_guarded(
        self,
        text: str,
        *,
        before_batch: Callable[[], bool],
    ) -> None:
        """Replace a selection only while every SendInput batch stays authorized."""

        normalized = self._validated_text(text)
        if normalized:
            self.insert(normalized, before_batch=before_batch)
            return
        self._send(
            [
                _key_event(VK_BACK, 0, 0),
                _key_event(VK_BACK, 0, KEYEVENTF_KEYUP),
            ],
            before_batch=before_batch,
        )
