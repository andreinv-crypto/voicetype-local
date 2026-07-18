from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import voicetype_local.__main__ as entrypoint


@dataclass
class _FakeActivation:
    is_primary: bool
    notified: int = 0
    closed: int = 0

    def notify_settings(self) -> bool:
        self.notified += 1
        return True

    def wait_for_settings(self, _timeout_ms: int) -> bool:
        return False

    def close(self) -> None:
        self.closed += 1


def _prepare_main(monkeypatch: pytest.MonkeyPatch, activation: _FakeActivation):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        entrypoint,
        "InstanceActivation",
        SimpleNamespace(acquire=lambda: activation),
    )
    monkeypatch.setattr(entrypoint.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(
        entrypoint,
        "run_app",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


@pytest.mark.parametrize(
    ("arguments", "open_settings"),
    [([], True), (["--background"], False)],
)
def test_primary_launch_preserves_foreground_or_background_intent(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    open_settings: bool,
) -> None:
    activation = _FakeActivation(is_primary=True)
    calls = _prepare_main(monkeypatch, activation)

    assert entrypoint.main(arguments) == 0

    assert len(calls) == 1
    assert calls[0]["open_settings_on_start"] is open_settings
    assert calls[0]["silent_start"] is (not open_settings)
    assert calls[0]["activation_waiter"] == activation.wait_for_settings
    assert activation.notified == 0
    assert activation.closed == 1


@pytest.mark.parametrize(
    ("arguments", "notifications"),
    [([], 1), (["--background"], 0)],
)
def test_secondary_launch_only_foreground_notifies_existing_instance(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    notifications: int,
) -> None:
    activation = _FakeActivation(is_primary=False)
    calls = _prepare_main(monkeypatch, activation)

    assert entrypoint.main(arguments) == 0

    assert calls == []
    assert activation.notified == notifications
    assert activation.closed == 1


def test_primary_handle_is_closed_when_app_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation = _FakeActivation(is_primary=True)
    _prepare_main(monkeypatch, activation)
    monkeypatch.setattr(
        entrypoint,
        "run_app",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("startup failed")),
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        entrypoint.main([])

    assert activation.closed == 1
