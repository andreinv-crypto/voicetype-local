from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import voicetype_local.memory as memory_module
from voicetype_local.memory import (
    SCHEMA_VERSION,
    AliasInput,
    MemoryChecksumError,
    MemoryConflictError,
    MemoryContext,
    MemoryOwnershipError,
    MemoryStore,
    MemoryValidationError,
    Scope,
    _MIGRATION_1,
    make_pack_document,
    open_memory_store_resilient,
    normalize_key,
    payload_checksum,
)


def _alias(text: str, *, auto_replace: bool = True) -> AliasInput:
    return AliasInput(text, auto_replace=auto_replace, confirmed=True)


def test_normalize_key_nfkc_casefold_keeps_diacritics() -> None:
    assert normalize_key("  ＣhatGPT\tPRO  ") == "chatgpt pro"
    assert normalize_key("SÍ") == "sí"
    assert normalize_key("SÍ") != normalize_key("SI")


def test_schema_foreign_keys_and_no_content_history(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        assert store.schema_version == SCHEMA_VERSION
        assert store.integrity_check()
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"terms", "aliases", "scopes", "style_preferences", "packs"} <= tables
        assert not any("transcript" in table or "audio" in table for table in tables)
    finally:
        store.close()


def test_only_explicit_confirmed_entries_are_persisted(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        with pytest.raises(MemoryValidationError, match="explicitly confirmed"):
            store.add_term("ChatGPT")
        with pytest.raises(MemoryValidationError, match="aliases"):
            store.add_term("ChatGPT", confirmed=True, aliases=["chat g p t"])
        assert store.list_terms() == []
        term = store.add_term(
            "ChatGPT",
            confirmed=True,
            aliases=[_alias("chat g p t")],
        )
        assert term.confirmed
        assert term.aliases[0].confirmed
    finally:
        store.close()


def test_duplicate_key_is_language_and_scope_aware(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        store.add_term("WordPress", language="en", confirmed=True)
        with pytest.raises(MemoryConflictError):
            store.add_term("ｗｏｒｄｐｒｅｓｓ", language="en", confirmed=True)
        store.add_term("WordPress", language="es", confirmed=True)
        store.add_term(
            "WordPress",
            language="en",
            confirmed=True,
            scopes=[Scope("app", "WINWORD.EXE")],
        )
        assert len(store.list_terms()) == 3
    finally:
        store.close()


def test_foreign_key_cascade_removes_aliases_and_scopes(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        term = store.add_term(
            "OpenAI",
            confirmed=True,
            aliases=[_alias("open ai")],
            scopes=[Scope("domain", "technology")],
        )
        store.delete_term(term.id)
        assert store._connection.execute(
            "SELECT count(*) FROM aliases WHERE term_id=?", (term.id,)
        ).fetchone()[0] == 0
        assert store._connection.execute(
            "SELECT count(*) FROM scopes WHERE term_id=?", (term.id,)
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_alias_style_and_pack_can_be_disabled_or_deleted(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        term = store.add_term("OpenAI", confirmed=True, aliases=[_alias("open ai")])
        alias_id = term.aliases[0].id
        store.delete_alias(alias_id)
        assert store.get_term(term.id).aliases == ()

        style = store.add_style_preference(
            "punctuation.mode",
            "minimal",
            confirmed=True,
        )
        store.set_style_enabled(style.id, False)
        assert store.select_styles() == []
        store.delete_style(style.id)
        with pytest.raises(KeyError):
            store.get_style(style.id)
    finally:
        store.close()


def test_store_rejects_worker_thread_access(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store.list_terms()
        except BaseException as exc:  # asserted below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    try:
        assert len(errors) == 1
        assert isinstance(errors[0], MemoryOwnershipError)
    finally:
        store.close()


def test_deterministic_source_and_scope_precedence(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        pack_term = store.add_term(
            "Flow",
            source="external_pack",
            priority=100,
            confirmed=True,
            aliases=[_alias("флоу")],
        )
        user_term = store.add_term(
            "Wispr Flow",
            source="user",
            priority=-100,
            confirmed=True,
            aliases=[_alias("флоу")],
        )
        app_term = store.add_term(
            "Flow App",
            source="user",
            priority=-200,
            confirmed=True,
            aliases=[_alias("приложение флоу")],
            scopes=[Scope("app", "chrome.exe")],
        )
        selected = store.select_terms(MemoryContext(app_id="chrome.exe"))
        assert [item.id for item in selected[:3]] == [app_term.id, user_term.id, pack_term.id]
        rules = store.replacement_candidates(draft="флоу")
        assert len(rules) == 1
        assert rules[0].canonical_text == "Wispr Flow"
    finally:
        store.close()


def test_disabled_pack_terms_are_not_selected(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        pack = {
            "pack_id": "technology.en",
            "name": "Technology",
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
                    "aliases": [
                        {
                            "text": "post gres q l",
                            "match_type": "token_sequence",
                            "auto_replace": True,
                            "confirmed": True,
                        }
                    ],
                    "scopes": [{"type": "domain", "value": "technology"}],
                }
            ],
        }
        store.install_pack(make_pack_document(pack))
        context = MemoryContext(language="en", domain="technology")
        assert [item.canonical_text for item in store.select_terms(context)] == ["PostgreSQL"]
        store.set_pack_enabled("technology.en", False)
        assert store.select_terms(context) == []
    finally:
        store.close()


def test_versioned_starter_pack_checksum_and_explicit_enable(tmp_path) -> None:
    pack_path = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "packs"
        / "technology-core.multilingual.v1.json"
    )
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        summary = store.install_pack(pack_path.read_bytes())
        assert summary.terms_added == 4
        context = MemoryContext(domain="technology")
        assert store.select_terms(context) == []
        store.set_pack_enabled("technology-core.multilingual", True)
        assert store.list_packs()[0].enabled is True
        selected = store.select_terms(context)
        assert {term.canonical_text for term in selected} == {
            "ChatGPT",
            "OpenAI",
            "PostgreSQL",
            "WordPress",
        }
        # Application startup may re-install a newer built-in pack. That must
        # not reset the user's explicit enabled/disabled choice.
        store.install_pack(pack_path.read_bytes())
        assert {term.canonical_text for term in store.select_terms(context)} == {
            "ChatGPT",
            "OpenAI",
            "PostgreSQL",
            "WordPress",
        }
    finally:
        store.close()


def test_export_import_checksum_validation_and_no_user_overwrite(tmp_path) -> None:
    source = MemoryStore(tmp_path / "source.sqlite3", enable_fts=False)
    destination = MemoryStore(tmp_path / "destination.sqlite3", enable_fts=False)
    try:
        source.add_term(
            "ChatGPT",
            confirmed=True,
            aliases=[_alias("чат джи пи ти")],
        )
        destination.add_term("ChatGPT", confirmed=True)
        exported = source.export_profile()
        summary = destination.import_profile(exported)
        assert summary.terms_added == 0
        assert summary.terms_skipped == 1
        assert destination.list_terms()[0].source == "user"

        tampered = json.loads(json.dumps(exported))
        tampered["payload"]["terms"][0]["canonical_text"] = "Malicious"
        with pytest.raises(MemoryChecksumError):
            destination.import_profile(tampered)
        assert len(destination.list_terms()) == 1

        unknown = json.loads(json.dumps(exported))
        unknown["payload"]["unknown"] = []
        unknown["checksum"] = payload_checksum(unknown["payload"])
        with pytest.raises(MemoryValidationError, match="unknown fields"):
            destination.import_profile(unknown)
    finally:
        source.close()
        destination.close()


def test_import_transaction_rolls_back_on_runtime_failure(tmp_path, monkeypatch) -> None:
    source = MemoryStore(tmp_path / "source.sqlite3", enable_fts=False)
    destination = MemoryStore(tmp_path / "destination.sqlite3", enable_fts=False)
    try:
        source.add_term("First", confirmed=True)
        source.add_term("Second", confirmed=True)
        document = source.export_profile()
        original = destination._insert_term_tx
        calls = 0

        def fail_second(connection, data, *, on_conflict):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic failure")
            return original(connection, data, on_conflict=on_conflict)

        monkeypatch.setattr(destination, "_insert_term_tx", fail_second)
        with pytest.raises(RuntimeError, match="synthetic"):
            destination.import_profile(document)
        assert destination.list_terms() == []
    finally:
        source.close()
        destination.close()


def test_migration_creates_backup_and_is_atomic(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_MIGRATION_1)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO schema_meta VALUES(1, 1, 'old', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    store = MemoryStore(path, enable_fts=False)
    try:
        assert store.schema_version == SCHEMA_VERSION
        assert store.last_migration_backup is not None
        assert store.last_migration_backup.exists()
        backup = sqlite3.connect(store.last_migration_backup)
        try:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        finally:
            backup.close()
    finally:
        store.close()


def test_failed_migration_rolls_back_and_leaves_v1_recoverable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_MIGRATION_1)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO schema_meta VALUES(1, 1, 'old', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setitem(
        memory_module._MIGRATIONS,
        2,
        "CREATE TABLE must_roll_back(id INTEGER); INVALID SQL",
    )

    with pytest.raises(memory_module.MemoryMigrationError):
        MemoryStore(path, enable_fts=False)

    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE name='must_roll_back'"
        ).fetchone() is None
    finally:
        check.close()


def test_optional_fts_and_plain_index_fallback_return_same_exact_alias(tmp_path) -> None:
    fallback = MemoryStore(tmp_path / "fallback.sqlite3", enable_fts=False)
    fts = MemoryStore(tmp_path / "fts.sqlite3", enable_fts=True)
    try:
        for store in (fallback, fts):
            store.add_term(
                "Torrevieja",
                language="es",
                confirmed=True,
                aliases=[_alias("torre vieja")],
            )
        context = MemoryContext(language="es")
        assert [item.canonical_text for item in fallback.search_relevant("torre vieja", context)] == [
            "Torrevieja"
        ]
        assert [item.canonical_text for item in fts.search_relevant("torre vieja", context)] == [
            "Torrevieja"
        ]
    finally:
        fallback.close()
        fts.close()


def test_backup_is_independent_and_valid(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", enable_fts=False)
    try:
        store.add_term("WordPress", confirmed=True)
        backup_path = store.backup(tmp_path / "memory.backup.sqlite3")
        store.add_term("OpenAI", confirmed=True)
        backup_store = MemoryStore(backup_path, enable_fts=False)
        try:
            assert [term.canonical_text for term in backup_store.list_terms()] == ["WordPress"]
        finally:
            backup_store.close()
    finally:
        store.close()


def test_corrupt_database_is_preserved_and_clean_store_recovers(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    path.write_bytes(b"not a sqlite database")

    store, recovery = open_memory_store_resilient(path, enable_fts=False)
    try:
        assert recovery is not None
        assert recovery.read_bytes() == b"not a sqlite database"
        assert store.integrity_check()
        assert store.list_terms() == []
    finally:
        store.close()
