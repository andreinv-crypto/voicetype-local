from __future__ import annotations

import inspect
import json

import pytest

from voicetype_local.memory import AliasInput, MemoryStore, Scope, make_pack_document
from voicetype_local.memory_ui import (
    MemoryUIController,
    MemoryUIValidationError,
    TermFormModel,
    TermFormValues,
    format_import_summary,
    open_memory_window,
)


def test_valid_form_builds_explicitly_confirmed_store_arguments() -> None:
    form = TermFormModel(
        TermFormValues(
            canonical="Codex",
            alias="кодекс",
            language="ru",
            priority="120",
            scope_type="domain",
            scope_value="example.com",
        )
    )

    arguments = form.store_kwargs()

    assert arguments["canonical_text"] == "Codex"
    assert arguments["confirmed"] is True
    assert arguments["source"] == "user"
    assert arguments["priority"] == 120
    assert arguments["aliases"] == (AliasInput("кодекс", confirmed=True),)
    assert arguments["scopes"] == (Scope("domain", "example.com"),)


@pytest.mark.parametrize(
    ("scope_type", "scope_value"),
    [
        ("global", ""),
        ("language", "es"),
        ("domain", "torrevieja-tur.com"),
        ("app", "chrome"),
    ],
)
def test_all_requested_scope_types_validate(scope_type: str, scope_value: str) -> None:
    form = TermFormModel(
        TermFormValues(canonical="Torrevieja", scope_type=scope_type, scope_value=scope_value)
    )

    assert form.store_kwargs()["scopes"] == (Scope(scope_type, scope_value),)


def test_scope_value_priority_and_canonical_are_validated_without_tk() -> None:
    form = TermFormModel(
        TermFormValues(
            canonical=" ",
            priority="1001",
            scope_type="domain",
            scope_value="",
        )
    )

    with pytest.raises(MemoryUIValidationError) as captured:
        form.store_kwargs()

    assert set(captured.value.errors) == {"canonical", "priority", "scope_value"}


def test_alias_is_optional_but_cannot_duplicate_canonical() -> None:
    without_alias = TermFormModel(TermFormValues(canonical="Whisper"))
    assert without_alias.store_kwargs()["aliases"] == ()

    duplicate = TermFormModel(TermFormValues(canonical="Whisper", alias="whisper"))
    assert "alias" in duplicate.validation_errors()


def test_controller_lists_and_searches_enabled_and_disabled_terms(tmp_path) -> None:
    with MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False) as store:
        first = store.add_term(
            "OpenAI",
            confirmed=True,
            aliases=(AliasInput("опенай", confirmed=True),),
        )
        second = store.add_term("Torrevieja", language="es", confirmed=True)
        store.set_term_enabled(second.id, False)
        controller = MemoryUIController(store)

        assert [term.id for term in controller.list_terms()] == [first.id, second.id]
        assert [term.id for term in controller.list_terms("опенай")] == [first.id]
        assert [term.id for term in controller.list_terms("torrevieja")] == [second.id]


def test_controller_adds_toggles_and_requires_delete_confirmation(tmp_path) -> None:
    with MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False) as store:
        controller = MemoryUIController(store)
        term = controller.add_term(TermFormModel(TermFormValues(canonical="VoiceType")))

        assert term.confirmed
        assert controller.toggle_enabled(term.id) is False
        assert store.get_term(term.id) is not None
        assert store.get_term(term.id).enabled is False  # type: ignore[union-attr]

        with pytest.raises(MemoryUIValidationError):
            controller.delete_term(term.id)
        with pytest.raises(MemoryUIValidationError):
            controller.delete_term(term.id, confirmed=1)  # type: ignore[arg-type]
        assert store.get_term(term.id) is not None

        controller.delete_term(term.id, confirmed=True)
        assert store.get_term(term.id) is None


def test_controller_lists_and_toggles_installed_packs_via_public_api(tmp_path) -> None:
    pack = {
        "pack_id": "technology.test",
        "name": "Technology Test",
        "version": "1.0.0",
        "language": "en",
        "source": "external_pack",
        "enabled": True,
        "terms": [
            {
                "canonical_text": "PostgreSQL",
                "language": "en",
                "kind": "product",
                "priority": 80,
                "confirmed": True,
                "enabled": True,
                "aliases": [],
                "scopes": [{"type": "global", "value": ""}],
            }
        ],
    }
    with MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False) as store:
        store.install_pack(make_pack_document(pack))
        controller = MemoryUIController(store)

        listed = controller.list_packs()
        assert [(item.pack_id, item.enabled) for item in listed] == [
            ("technology.test", True)
        ]
        assert controller.toggle_pack_enabled("technology.test") is False
        assert store.list_packs()[0].enabled is False


def test_import_merges_and_never_overwrites_existing_user_term(tmp_path) -> None:
    destination = MemoryStore(tmp_path / "destination.sqlite3", enable_fts=False)
    source = MemoryStore(tmp_path / "source.sqlite3", enable_fts=False)
    try:
        existing = destination.add_term("Codex", priority=900, confirmed=True)
        source.add_term("Codex", priority=-10, confirmed=True)
        source.add_term("Whisper", priority=20, confirmed=True)
        profile = source.export_to_file(tmp_path / "profile.json")

        summary = MemoryUIController(destination).import_file(profile)

        unchanged = destination.get_term(existing.id)
        assert unchanged is not None
        assert unchanged.priority == 900
        assert summary.terms_added == 1
        assert summary.terms_skipped == 1
        assert {term.canonical_text for term in destination.list_terms()} == {
            "Codex",
            "Whisper",
        }
    finally:
        source.close()
        destination.close()


def test_export_contains_only_structured_memory_profile_sections(tmp_path) -> None:
    with MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False) as store:
        store.add_term("VoiceType", confirmed=True)
        destination = MemoryUIController(store).export_file(tmp_path / "memory.json")

    document = json.loads(destination.read_text(encoding="utf-8"))
    assert set(document) == {"format", "version", "exported_at", "payload", "checksum"}
    assert set(document["payload"]) == {"terms", "styles", "packs"}
    assert set(document["payload"]["terms"][0]) == {
        "canonical_text",
        "language",
        "kind",
        "priority",
        "source",
        "confirmed",
        "enabled",
        "aliases",
        "scopes",
        "pack_ids",
    }


def test_import_summary_reports_counts_only() -> None:
    from voicetype_local.memory import ImportSummary

    message = format_import_summary(
        ImportSummary(
            terms_added=2,
            terms_skipped=3,
            styles_added=1,
            styles_skipped=4,
            packs_added=1,
        )
    )

    assert message == "Импорт завершён: добавлено 4, пропущено существующих 7."
    assert "term" not in message.casefold()
    assert "audio" not in message.casefold()
    assert "secret" not in message.casefold()


def test_open_api_is_explicit_and_accepts_optional_transfer_callbacks() -> None:
    parameters = inspect.signature(open_memory_window).parameters

    assert tuple(parameters) == (
        "parent",
        "store",
        "import_callback",
        "export_callback",
        "on_close",
    )
    assert parameters["import_callback"].default is None
    assert parameters["export_callback"].default is None
