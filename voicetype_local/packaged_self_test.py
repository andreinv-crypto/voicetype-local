from __future__ import annotations

"""Import-only checks used before and after packaging.

This module deliberately does not instantiate UI Automation, enumerate the
desktop, open a window, register a hook, or execute a VoiceType command.
"""

import importlib
from collections.abc import Callable
from importlib import metadata


EXPECTED_UIAUTOMATION_VERSION = "2.0.29"

REQUIRED_CONTROL_MODULES = (
    "uiautomation",
    "voicetype_local.windows_control",
    "voicetype_local.voice_control",
    "voicetype_local.voice_commands",
    "voicetype_local.dictation_transform",
    "voicetype_local.session_editing",
    "voicetype_local.session_editor",
    "voicetype_local.windows_session_editor_probe",
)

REQUIRED_CONTROL_SYMBOLS = {
    "voicetype_local.windows_control": (
        "UiaControlBackend",
        "WindowsControlExecutor",
    ),
    "voicetype_local.voice_control": ("VoiceControlRouter",),
    "voicetype_local.voice_commands": ("VoiceCommandParser",),
    "voicetype_local.dictation_transform": ("transform_dictation",),
    "voicetype_local.session_editing": ("SessionEditCommandParser",),
    "voicetype_local.session_editor": ("SessionTextEditor",),
    "voicetype_local.windows_session_editor_probe": (
        "run_isolated_session_editor_probe",
    ),
}


def run_packaged_control_self_test(
    *,
    importer: Callable[[str], object] = importlib.import_module,
    version_reader: Callable[[str], str] = metadata.version,
) -> tuple[int, str]:
    """Return a stable exit code and metadata-only diagnostic label."""

    imported: dict[str, object] = {}
    for index, module_name in enumerate(REQUIRED_CONTROL_MODULES):
        try:
            imported[module_name] = importer(module_name)
        except Exception as exc:
            return 30 + index, f"import:{module_name}:{type(exc).__name__}"

    for index, (module_name, symbols) in enumerate(REQUIRED_CONTROL_SYMBOLS.items()):
        module = imported[module_name]
        if any(not hasattr(module, symbol) for symbol in symbols):
            return 40 + index, f"symbols:{module_name}"

    try:
        installed_version = version_reader("uiautomation")
    except Exception as exc:
        return 50, f"metadata:uiautomation:{type(exc).__name__}"
    if installed_version != EXPECTED_UIAUTOMATION_VERSION:
        return 51, "version:uiautomation"

    return 0, "ok"
