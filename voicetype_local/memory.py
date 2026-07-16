from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import state_dir


SCHEMA_VERSION = 2
PROFILE_FORMAT = "voicetype-memory"
PROFILE_VERSION = 1
PACK_FORMAT = "voicetype-pack"
PACK_VERSION = 1

MAX_TERM_LENGTH = 256
MAX_ALIAS_LENGTH = 256
MAX_STYLE_VALUE_LENGTH = 1024
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_TERMS = 10_000
MAX_IMPORT_STYLES = 1_000
MAX_ALIASES_PER_TERM = 32
MAX_SCOPES_PER_TERM = 16
MAX_PACKS_PER_IMPORT = 256

TERM_KINDS = {
    "term",
    "name",
    "abbreviation",
    "product",
    "organization",
    "place",
    "other",
}
TERM_SOURCES = {"user", "import", "built_in_pack", "external_pack"}
PACK_SOURCES = {"built_in_pack", "external_pack"}
SCOPE_TYPES = {"global", "language", "domain", "app"}
MATCH_TYPES = {"exact", "token_sequence"}

SOURCE_RANK = {
    "built_in_pack": 1,
    "external_pack": 2,
    "import": 3,
    "user": 4,
}
SCOPE_RANK = {"global": 1, "language": 2, "domain": 3, "app": 4}

_LANGUAGE_RE = re.compile(r"^(?:und|[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2}|-\d{3})?)$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_STYLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MemoryStoreError(RuntimeError):
    pass


class MemoryValidationError(MemoryStoreError, ValueError):
    pass


class MemoryConflictError(MemoryStoreError):
    pass


class MemoryOwnershipError(MemoryStoreError):
    pass


class MemoryChecksumError(MemoryValidationError):
    pass


class MemoryMigrationError(MemoryStoreError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_key(value: str) -> str:
    """Create a comparison key without changing the stored spelling.

    NFKC and casefold are deliberately used only for the technical key.
    Diacritics are retained: ``si`` and ``sí`` are different keys.
    """

    if not isinstance(value, str):
        raise MemoryValidationError("text value must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    return normalized.casefold()


def _clean_display_text(value: Any, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise MemoryValidationError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise MemoryValidationError(f"{field_name} cannot be empty")
    if value != value.strip():
        raise MemoryValidationError(f"{field_name} cannot have outer whitespace")
    if len(value) > max_length:
        raise MemoryValidationError(f"{field_name} is too long")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise MemoryValidationError(f"{field_name} contains control characters")
    if "\x00" in value:
        raise MemoryValidationError(f"{field_name} contains NUL")
    return value


def _normalize_language(value: Any) -> str:
    if value in {None, "", "auto"}:
        return "und"
    if not isinstance(value, str):
        raise MemoryValidationError("language must be a string")
    parts = value.replace("_", "-").split("-")
    normalized_parts: list[str] = []
    for index, part in enumerate(parts):
        if index == 0:
            normalized_parts.append(part.lower())
        elif len(part) == 4:
            normalized_parts.append(part.title())
        else:
            normalized_parts.append(part.upper())
    result = "-".join(normalized_parts)
    if not _LANGUAGE_RE.fullmatch(result):
        raise MemoryValidationError("invalid language code")
    return result


def _bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise MemoryValidationError(f"{field_name} must be a boolean")
    return value


def _priority(value: Any) -> int:
    if type(value) is not int or not -1000 <= value <= 1000:
        raise MemoryValidationError("priority must be an integer from -1000 to 1000")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_loads_strict(value: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise MemoryValidationError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            MemoryValidationError(f"invalid JSON number: {constant}")
        ),
    )


def payload_checksum(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    where: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise MemoryValidationError(f"{where} field names must be strings")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise MemoryValidationError(f"{where} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise MemoryValidationError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")


@dataclass(frozen=True, slots=True, order=True)
class Scope:
    scope_type: str = "global"
    value: str = ""

    def __post_init__(self) -> None:
        if self.scope_type not in SCOPE_TYPES:
            raise MemoryValidationError("invalid scope type")
        value = self.value
        if not isinstance(value, str) or len(value) > 256:
            raise MemoryValidationError("invalid scope value")
        if self.scope_type == "global":
            if value:
                raise MemoryValidationError("global scope must have an empty value")
        elif not value:
            raise MemoryValidationError(f"{self.scope_type} scope requires a value")
        if self.scope_type == "language":
            if _normalize_language(value) == "und":
                raise MemoryValidationError("language scope requires a concrete language")
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise MemoryValidationError("scope contains control characters")

    @property
    def specificity(self) -> int:
        return SCOPE_RANK[self.scope_type]

    @property
    def normalized_value(self) -> str:
        if self.scope_type == "global":
            return ""
        if self.scope_type == "language":
            return _normalize_language(self.value)
        return normalize_key(self.value)


@dataclass(frozen=True, slots=True)
class AliasInput:
    text: str
    match_type: str = "token_sequence"
    auto_replace: bool = True
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class AliasRecord:
    id: int
    term_id: int
    alias_text: str
    normalized_key: str
    match_type: str
    auto_replace: bool
    confirmed: bool


@dataclass(frozen=True, slots=True)
class TermRecord:
    id: int
    canonical_text: str
    normalized_key: str
    language: str
    kind: str
    priority: int
    source: str
    confirmed: bool
    enabled: bool
    created_at: str
    updated_at: str
    last_used_at: str | None
    use_count: int
    aliases: tuple[AliasRecord, ...] = ()
    scopes: tuple[Scope, ...] = ()
    pack_ids: tuple[str, ...] = ()
    matched_scope_rank: int = 0

    @property
    def precedence(self) -> tuple[int, int, int, str]:
        return (
            SOURCE_RANK[self.source],
            self.matched_scope_rank,
            self.priority,
            self.updated_at,
        )


@dataclass(frozen=True, slots=True)
class StylePreference:
    id: int
    key: str
    value: str
    scope: Scope
    source: str
    confirmed: bool
    enabled: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class PackRecord:
    pack_id: str
    name: str
    version: str
    language: str
    source: str
    checksum: str
    enabled: bool
    installed_at: str


@dataclass(frozen=True, slots=True)
class MemoryContext:
    language: str | None = None
    domain: str | None = None
    app_id: str | None = None

    def normalized_language(self) -> str | None:
        if self.language in {None, "", "auto", "und"}:
            return None
        return _normalize_language(self.language)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    terms_added: int = 0
    terms_skipped: int = 0
    styles_added: int = 0
    styles_skipped: int = 0
    packs_added: int = 0


@dataclass(frozen=True, slots=True)
class ReplacementCandidate:
    alias: str
    normalized_alias: str
    canonical_text: str
    term_id: int
    match_type: str
    precedence: tuple[int, int, int, str]


_MIGRATION_1 = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    app_version TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);

CREATE TABLE terms (
    id INTEGER PRIMARY KEY,
    canonical_text TEXT NOT NULL CHECK (length(canonical_text) BETWEEN 1 AND 256),
    normalized_key TEXT NOT NULL CHECK (length(normalized_key) BETWEEN 1 AND 256),
    language TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('term','name','abbreviation','product','organization','place','other')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN -1000 AND 1000),
    source TEXT NOT NULL CHECK (source IN ('user','import','built_in_pack','external_pack')),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
);

CREATE TABLE aliases (
    id INTEGER PRIMARY KEY,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    alias_text TEXT NOT NULL CHECK (length(alias_text) BETWEEN 1 AND 256),
    normalized_key TEXT NOT NULL CHECK (length(normalized_key) BETWEEN 1 AND 256),
    match_type TEXT NOT NULL CHECK (match_type IN ('exact','token_sequence')),
    auto_replace INTEGER NOT NULL CHECK (auto_replace IN (0,1)),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
    UNIQUE(term_id, normalized_key, match_type)
);

CREATE TABLE scopes (
    id INTEGER PRIMARY KEY,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('global','language','domain','app')),
    scope_value TEXT NOT NULL,
    CHECK ((scope_type = 'global' AND scope_value = '') OR
           (scope_type <> 'global' AND length(scope_value) BETWEEN 1 AND 256)),
    UNIQUE(term_id, scope_type, scope_value)
);

CREATE TABLE style_preferences (
    id INTEGER PRIMARY KEY,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL CHECK (length(preference_value) BETWEEN 1 AND 1024),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('global','language','domain','app')),
    scope_value TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user','import','built_in_pack','external_pack')),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    updated_at TEXT NOT NULL,
    UNIQUE(preference_key, scope_type, scope_value, source)
);

CREATE TABLE packs (
    pack_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    language TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('built_in_pack','external_pack')),
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    installed_at TEXT NOT NULL
);

CREATE TABLE pack_terms (
    pack_id TEXT NOT NULL REFERENCES packs(pack_id) ON DELETE CASCADE,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    PRIMARY KEY(pack_id, term_id)
);

CREATE TABLE memory_change_log (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('term','alias','style','pack')),
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('created','updated','enabled','disabled','deleted','imported')),
    changed_at TEXT NOT NULL
);
"""

_MIGRATION_2 = """
CREATE INDEX idx_terms_lookup ON terms(normalized_key, language, enabled, confirmed);
CREATE INDEX idx_terms_selection ON terms(enabled, confirmed, source, priority);
CREATE INDEX idx_alias_lookup ON aliases(normalized_key, auto_replace, confirmed);
CREATE INDEX idx_scopes_lookup ON scopes(scope_type, scope_value, term_id);
CREATE INDEX idx_styles_lookup ON style_preferences(preference_key, enabled, confirmed);
CREATE INDEX idx_pack_terms_term ON pack_terms(term_id, pack_id);
"""

_MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2}


class MemoryStore:
    """Single-process owner of VoiceType's explicit structured memory.

    The store never accepts or exposes transcript/audio persistence APIs.  A
    process and thread guard prevents inference workers from sharing its SQLite
    connection; callers pass workers only compact immutable records instead.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        app_version: str = "0",
        enable_fts: bool = True,
        backup_before_migration: bool = True,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else state_dir() / "data" / "memory.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.app_version = str(app_version)
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._closed = False
        self.last_migration_backup: Path | None = None
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=True,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self._migrate(backup_before_migration=backup_before_migration)
            self.fts_available = self._enable_fts() if enable_fts else False
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        self._assert_owner()
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def _assert_owner(self) -> None:
        if self._closed:
            raise MemoryStoreError("memory store is closed")
        if os.getpid() != self._owner_pid or threading.get_ident() != self._owner_thread:
            raise MemoryOwnershipError("memory.sqlite3 may only be used by its owner process/thread")

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = DELETE")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._connection.execute("PRAGMA trusted_schema = OFF")
        except sqlite3.DatabaseError:
            pass
        foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
        if foreign_keys != 1:
            raise MemoryStoreError("SQLite foreign keys could not be enabled")

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._assert_owner()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _migrate(self, *, backup_before_migration: bool) -> None:
        old_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if old_version > SCHEMA_VERSION:
            raise MemoryMigrationError(
                f"memory schema {old_version} is newer than supported {SCHEMA_VERSION}"
            )
        if old_version == SCHEMA_VERSION:
            self._verify_schema_meta()
            return
        has_existing_schema = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone() is not None
        if has_existing_schema and backup_before_migration:
            self.last_migration_backup = self.backup(
                self.path.with_name(
                    f"{self.path.stem}.before-v{old_version}-to-v{SCHEMA_VERSION}.backup.sqlite3"
                ),
                overwrite=True,
            )
        try:
            with self._write() as connection:
                for version in range(old_version + 1, SCHEMA_VERSION + 1):
                    script = _MIGRATIONS.get(version)
                    if script is None:
                        raise MemoryMigrationError(f"missing migration {version}")
                    for statement in script.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {version}")
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO schema_meta(singleton, schema_version, app_version, migrated_at)
                    VALUES(1, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        schema_version=excluded.schema_version,
                        app_version=excluded.app_version,
                        migrated_at=excluded.migrated_at
                    """,
                    (SCHEMA_VERSION, self.app_version, now),
                )
        except (sqlite3.DatabaseError, MemoryStoreError) as exc:
            raise MemoryMigrationError(f"memory migration failed: {exc}") from exc
        self._verify_schema_meta()

    def _verify_schema_meta(self) -> None:
        try:
            row = self._connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton=1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise MemoryMigrationError("schema_meta is missing or invalid") from exc
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise MemoryMigrationError("schema version metadata does not match PRAGMA user_version")
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MemoryMigrationError("foreign-key violations detected after migration")

    def _enable_fts(self) -> bool:
        try:
            with self._write() as connection:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS term_search_fts
                    USING fts5(term_id UNINDEXED, search_text,
                               tokenize='unicode61 remove_diacritics 0')
                    """
                )
                self._rebuild_fts_tx(connection)
        except sqlite3.DatabaseError:
            return False
        return True

    def _rebuild_fts_tx(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM term_search_fts")
        rows = connection.execute(
            """
            SELECT t.id, t.canonical_text,
                   COALESCE(group_concat(a.alias_text, ' '), '') AS alias_texts
            FROM terms t LEFT JOIN aliases a ON a.term_id=t.id AND a.confirmed=1
            GROUP BY t.id
            """
        ).fetchall()
        connection.executemany(
            "INSERT INTO term_search_fts(term_id, search_text) VALUES(?, ?)",
            ((row["id"], f"{row['canonical_text']} {row['alias_texts']}") for row in rows),
        )

    def _refresh_fts_term_tx(self, connection: sqlite3.Connection, term_id: int) -> None:
        if not self.fts_available:
            return
        connection.execute("DELETE FROM term_search_fts WHERE term_id=?", (term_id,))
        row = connection.execute(
            """
            SELECT t.canonical_text,
                   COALESCE(group_concat(a.alias_text, ' '), '') AS alias_texts
            FROM terms t LEFT JOIN aliases a ON a.term_id=t.id AND a.confirmed=1
            WHERE t.id=? GROUP BY t.id
            """,
            (term_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                "INSERT INTO term_search_fts(term_id, search_text) VALUES(?, ?)",
                (term_id, f"{row['canonical_text']} {row['alias_texts']}"),
            )

    def backup(self, destination: Path | None = None, *, overwrite: bool = False) -> Path:
        self._assert_owner()
        if destination is None:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            destination = self.path.with_name(f"{self.path.stem}.{timestamp}.backup.sqlite3")
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise MemoryValidationError("backup destination must differ from the database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        if destination.exists():
            destination.unlink()
        backup_connection = sqlite3.connect(destination)
        try:
            self._connection.backup(backup_connection)
            violations = backup_connection.execute("PRAGMA integrity_check").fetchone()
            if violations is None or violations[0] != "ok":
                raise MemoryStoreError("backup integrity check failed")
            if backup_connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MemoryStoreError("backup foreign-key check failed")
        finally:
            backup_connection.close()
        return destination

    def close(self) -> None:
        if self._closed:
            return
        self._assert_owner()
        self._connection.close()
        self._closed = True

    def integrity_check(self) -> bool:
        self._assert_owner()
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"

    @staticmethod
    def _validate_scope_list(scopes: Iterable[Scope | Mapping[str, Any]] | None) -> tuple[Scope, ...]:
        if scopes is None:
            return (Scope(),)
        parsed: list[Scope] = []
        for raw in scopes:
            if isinstance(raw, Scope):
                scope = raw
            elif isinstance(raw, Mapping):
                _require_exact_keys(
                    raw,
                    required={"type", "value"},
                    where="scope",
                )
                scope = Scope(raw["type"], raw["value"])
            else:
                raise MemoryValidationError("scope must be a Scope or object")
            parsed.append(scope)
        if not parsed:
            parsed.append(Scope())
        unique_by_key: dict[tuple[str, str], Scope] = {}
        for scope in parsed:
            unique_by_key.setdefault(
                (scope.scope_type, scope.normalized_value),
                scope,
            )
        unique = tuple(sorted(unique_by_key.values()))
        if len(unique) > MAX_SCOPES_PER_TERM:
            raise MemoryValidationError("too many scopes for one term")
        return unique

    @staticmethod
    def _validate_alias_list(
        aliases: Iterable[AliasInput | str | Mapping[str, Any]] | None,
        *,
        require_confirmed: bool,
    ) -> tuple[AliasInput, ...]:
        if aliases is None:
            return ()
        parsed: list[AliasInput] = []
        seen: set[tuple[str, str]] = set()
        for raw in aliases:
            if isinstance(raw, AliasInput):
                alias = raw
            elif isinstance(raw, str):
                alias = AliasInput(raw, confirmed=False)
            elif isinstance(raw, Mapping):
                _require_exact_keys(
                    raw,
                    required={"text", "match_type", "auto_replace", "confirmed"},
                    where="alias",
                )
                alias = AliasInput(
                    text=raw["text"],
                    match_type=raw["match_type"],
                    auto_replace=_bool(raw["auto_replace"], "alias.auto_replace"),
                    confirmed=_bool(raw["confirmed"], "alias.confirmed"),
                )
            else:
                raise MemoryValidationError("invalid alias")
            text = _clean_display_text(
                alias.text,
                field_name="alias.text",
                max_length=MAX_ALIAS_LENGTH,
            )
            if alias.match_type not in MATCH_TYPES:
                raise MemoryValidationError("invalid alias match type")
            if require_confirmed and alias.confirmed is not True:
                raise MemoryValidationError("aliases must be explicitly confirmed")
            normalized = normalize_key(text)
            if not normalized:
                raise MemoryValidationError("alias normalizes to an empty value")
            if len(normalized) > MAX_ALIAS_LENGTH:
                raise MemoryValidationError("normalized alias is too long")
            key = (normalized, alias.match_type)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(
                AliasInput(text, alias.match_type, bool(alias.auto_replace), bool(alias.confirmed))
            )
        if len(parsed) > MAX_ALIASES_PER_TERM:
            raise MemoryValidationError("too many aliases for one term")
        return tuple(parsed)

    def add_term(
        self,
        canonical_text: str,
        *,
        language: str = "und",
        kind: str = "term",
        priority: int = 0,
        source: str = "user",
        confirmed: bool = False,
        enabled: bool = True,
        aliases: Iterable[AliasInput | str | Mapping[str, Any]] | None = None,
        scopes: Iterable[Scope | Mapping[str, Any]] | None = None,
    ) -> TermRecord:
        data = self._validate_term_data(
            {
                "canonical_text": canonical_text,
                "language": language,
                "kind": kind,
                "priority": priority,
                "source": source,
                "confirmed": confirmed,
                "enabled": enabled,
                "aliases": list(aliases or ()),
                "scopes": list(scopes or (Scope(),)),
                "pack_ids": [],
            },
            import_source=False,
        )
        with self._write() as connection:
            term_id = self._insert_term_tx(connection, data, on_conflict="error")
            self._log_tx(connection, "term", term_id, "created")
        record = self.get_term(term_id)
        assert record is not None
        return record

    def _validate_term_data(
        self, raw: Mapping[str, Any], *, import_source: bool
    ) -> dict[str, Any]:
        canonical = _clean_display_text(
            raw.get("canonical_text"),
            field_name="canonical_text",
            max_length=MAX_TERM_LENGTH,
        )
        language = _normalize_language(raw.get("language", "und"))
        kind = raw.get("kind", "term")
        if kind not in TERM_KINDS:
            raise MemoryValidationError("invalid term kind")
        source = raw.get("source", "import" if import_source else "user")
        if source not in TERM_SOURCES:
            raise MemoryValidationError("invalid term source")
        if import_source and source == "user":
            # Imported user records remain below locally-created user entries.
            source = "import"
        confirmed = _bool(raw.get("confirmed", False), "confirmed")
        if not confirmed:
            raise MemoryValidationError("terms must be explicitly confirmed")
        enabled = _bool(raw.get("enabled", True), "enabled")
        aliases = self._validate_alias_list(
            raw.get("aliases", ()), require_confirmed=True
        )
        scopes = self._validate_scope_list(raw.get("scopes", (Scope(),)))
        pack_ids_raw = raw.get("pack_ids", ())
        if not isinstance(pack_ids_raw, Sequence) or isinstance(pack_ids_raw, (str, bytes)):
            raise MemoryValidationError("pack_ids must be a list")
        pack_ids: list[str] = []
        for pack_id in pack_ids_raw:
            if not isinstance(pack_id, str) or not _IDENTIFIER_RE.fullmatch(pack_id):
                raise MemoryValidationError("invalid pack id")
            pack_ids.append(pack_id)
        normalized_key = normalize_key(canonical)
        if len(normalized_key) > MAX_TERM_LENGTH:
            raise MemoryValidationError("normalized canonical text is too long")
        return {
            "canonical_text": canonical,
            "normalized_key": normalized_key,
            "language": language,
            "kind": kind,
            "priority": _priority(raw.get("priority", 0)),
            "source": source,
            "confirmed": confirmed,
            "enabled": enabled,
            "aliases": aliases,
            "scopes": scopes,
            "pack_ids": tuple(sorted(set(pack_ids))),
        }

    @staticmethod
    def _scope_signature(scopes: Sequence[Scope]) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted((scope.scope_type, scope.normalized_value) for scope in scopes)
        )

    def _find_duplicate_tx(
        self,
        connection: sqlite3.Connection,
        *,
        normalized_key: str,
        language: str,
        scopes: Sequence[Scope],
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT id, source FROM terms WHERE normalized_key=? AND language=?",
            (normalized_key, language),
        ).fetchall()
        expected = self._scope_signature(scopes)
        for row in rows:
            actual_rows = connection.execute(
                "SELECT scope_type, scope_value FROM scopes WHERE term_id=? ORDER BY scope_type, scope_value",
                (row["id"],),
            ).fetchall()
            actual = self._scope_signature(
                tuple(Scope(item["scope_type"], item["scope_value"]) for item in actual_rows)
            )
            if actual == expected:
                return row
        return None

    def _insert_term_tx(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        *,
        on_conflict: str,
    ) -> int:
        duplicate = self._find_duplicate_tx(
            connection,
            normalized_key=data["normalized_key"],
            language=data["language"],
            scopes=data["scopes"],
        )
        if duplicate is not None:
            if on_conflict == "skip":
                return -int(duplicate["id"])
            raise MemoryConflictError("a term with the same language and scopes already exists")
        now = utc_now()
        cursor = connection.execute(
            """
            INSERT INTO terms(
                canonical_text, normalized_key, language, kind, priority, source,
                confirmed, enabled, created_at, updated_at, use_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                data["canonical_text"],
                data["normalized_key"],
                data["language"],
                data["kind"],
                data["priority"],
                data["source"],
                int(data["confirmed"]),
                int(data["enabled"]),
                now,
                now,
            ),
        )
        term_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO scopes(term_id, scope_type, scope_value) VALUES(?, ?, ?)",
            ((term_id, scope.scope_type, scope.value) for scope in data["scopes"]),
        )
        connection.executemany(
            """
            INSERT INTO aliases(
                term_id, alias_text, normalized_key, match_type, auto_replace, confirmed
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    term_id,
                    alias.text,
                    normalize_key(alias.text),
                    alias.match_type,
                    int(alias.auto_replace),
                    int(alias.confirmed),
                )
                for alias in data["aliases"]
            ),
        )
        for pack_id in data.get("pack_ids", ()):
            connection.execute(
                "INSERT INTO pack_terms(pack_id, term_id) VALUES(?, ?)",
                (pack_id, term_id),
            )
        self._refresh_fts_term_tx(connection, term_id)
        return term_id

    def add_alias(
        self,
        term_id: int,
        alias_text: str,
        *,
        match_type: str = "token_sequence",
        auto_replace: bool = True,
        confirmed: bool = False,
    ) -> AliasRecord:
        aliases = self._validate_alias_list(
            [AliasInput(alias_text, match_type, auto_replace, confirmed)],
            require_confirmed=True,
        )
        alias = aliases[0]
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM terms WHERE id=?", (term_id,)).fetchone() is None:
                raise KeyError(term_id)
            cursor = connection.execute(
                """
                INSERT INTO aliases(term_id, alias_text, normalized_key, match_type,
                                    auto_replace, confirmed)
                VALUES(?, ?, ?, ?, ?, 1)
                """,
                (
                    term_id,
                    alias.text,
                    normalize_key(alias.text),
                    alias.match_type,
                    int(alias.auto_replace),
                ),
            )
            alias_id = int(cursor.lastrowid)
            connection.execute("UPDATE terms SET updated_at=? WHERE id=?", (utc_now(), term_id))
            self._refresh_fts_term_tx(connection, term_id)
            self._log_tx(connection, "alias", alias_id, "created")
        return AliasRecord(
            alias_id,
            term_id,
            alias.text,
            normalize_key(alias.text),
            alias.match_type,
            alias.auto_replace,
            True,
        )

    def delete_alias(self, alias_id: int) -> None:
        with self._write() as connection:
            row = connection.execute(
                "SELECT term_id FROM aliases WHERE id=?", (alias_id,)
            ).fetchone()
            if row is None:
                raise KeyError(alias_id)
            term_id = int(row["term_id"])
            connection.execute("DELETE FROM aliases WHERE id=?", (alias_id,))
            connection.execute("UPDATE terms SET updated_at=? WHERE id=?", (utc_now(), term_id))
            self._refresh_fts_term_tx(connection, term_id)
            self._log_tx(connection, "alias", alias_id, "deleted")

    def get_term(self, term_id: int) -> TermRecord | None:
        self._assert_owner()
        row = self._connection.execute("SELECT * FROM terms WHERE id=?", (term_id,)).fetchone()
        return self._term_from_row(row) if row is not None else None

    def _term_from_row(self, row: sqlite3.Row, *, matched_scope_rank: int = 0) -> TermRecord:
        aliases = tuple(
            AliasRecord(
                int(item["id"]),
                int(item["term_id"]),
                item["alias_text"],
                item["normalized_key"],
                item["match_type"],
                bool(item["auto_replace"]),
                bool(item["confirmed"]),
            )
            for item in self._connection.execute(
                "SELECT * FROM aliases WHERE term_id=? ORDER BY normalized_key, id",
                (row["id"],),
            )
        )
        scopes = tuple(
            Scope(item["scope_type"], item["scope_value"])
            for item in self._connection.execute(
                "SELECT scope_type, scope_value FROM scopes WHERE term_id=? ORDER BY scope_type, scope_value",
                (row["id"],),
            )
        )
        pack_ids = tuple(
            item[0]
            for item in self._connection.execute(
                "SELECT pack_id FROM pack_terms WHERE term_id=? ORDER BY pack_id",
                (row["id"],),
            )
        )
        return TermRecord(
            id=int(row["id"]),
            canonical_text=row["canonical_text"],
            normalized_key=row["normalized_key"],
            language=row["language"],
            kind=row["kind"],
            priority=int(row["priority"]),
            source=row["source"],
            confirmed=bool(row["confirmed"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            use_count=int(row["use_count"]),
            aliases=aliases,
            scopes=scopes,
            pack_ids=pack_ids,
            matched_scope_rank=matched_scope_rank,
        )

    def list_terms(self, *, include_disabled: bool = False) -> list[TermRecord]:
        self._assert_owner()
        sql = "SELECT * FROM terms WHERE confirmed=1"
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY normalized_key, language, id"
        return [self._term_from_row(row) for row in self._connection.execute(sql)]

    def find_terms(
        self,
        query: str,
        context: MemoryContext | None = None,
        *,
        limit: int = 50,
    ) -> list[TermRecord]:
        return self.search_relevant(query, context, limit=limit)

    def set_term_enabled(self, term_id: int, enabled: bool) -> None:
        _bool(enabled, "enabled")
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE terms SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), term_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(term_id)
            self._log_tx(connection, "term", term_id, "enabled" if enabled else "disabled")

    def delete_term(self, term_id: int) -> None:
        with self._write() as connection:
            cursor = connection.execute("DELETE FROM terms WHERE id=?", (term_id,))
            if cursor.rowcount != 1:
                raise KeyError(term_id)
            if self.fts_available:
                connection.execute("DELETE FROM term_search_fts WHERE term_id=?", (term_id,))
            self._log_tx(connection, "term", term_id, "deleted")

    def _scope_match_rank(self, scopes: Sequence[Scope], context: MemoryContext) -> int:
        language = context.normalized_language()
        ranks: list[int] = []
        for scope in scopes:
            if scope.scope_type == "global":
                ranks.append(SCOPE_RANK["global"])
            elif scope.scope_type == "language" and language == _normalize_language(scope.value):
                ranks.append(SCOPE_RANK["language"])
            elif (
                scope.scope_type == "domain"
                and context.domain is not None
                and normalize_key(context.domain) == scope.normalized_value
            ):
                ranks.append(SCOPE_RANK["domain"])
            elif (
                scope.scope_type == "app"
                and context.app_id is not None
                and normalize_key(context.app_id) == scope.normalized_value
            ):
                ranks.append(SCOPE_RANK["app"])
        return max(ranks, default=0)

    def select_terms(
        self,
        context: MemoryContext | None = None,
        *,
        limit: int = 128,
        global_min_priority: int | None = None,
        term_ids: set[int] | None = None,
    ) -> list[TermRecord]:
        self._assert_owner()
        context = context or MemoryContext()
        limit = max(0, min(int(limit), 1000))
        rows = self._connection.execute(
            """
            SELECT t.*, s.scope_type AS selected_scope_type,
                   s.scope_value AS selected_scope_value
            FROM terms t JOIN scopes s ON s.term_id=t.id
            WHERE t.confirmed=1 AND t.enabled=1
              AND (
                  NOT EXISTS (SELECT 1 FROM pack_terms pt WHERE pt.term_id=t.id)
                  OR EXISTS (
                      SELECT 1 FROM pack_terms pt JOIN packs p ON p.pack_id=pt.pack_id
                      WHERE pt.term_id=t.id AND p.enabled=1
                  )
              )
            """
        ).fetchall()
        selected_rows: dict[int, tuple[sqlite3.Row, int]] = {}
        language = context.normalized_language()
        for row in rows:
            term_id = int(row["id"])
            if term_ids is not None and term_id not in term_ids:
                continue
            if row["language"] != "und" and language is not None and row["language"] != language:
                continue
            rank = self._scope_match_rank(
                (Scope(row["selected_scope_type"], row["selected_scope_value"]),),
                context,
            )
            if not rank:
                continue
            if (
                global_min_priority is not None
                and rank == SCOPE_RANK["global"]
                and int(row["priority"]) < global_min_priority
            ):
                continue
            existing = selected_rows.get(term_id)
            if existing is None or rank > existing[1]:
                selected_rows[term_id] = (row, rank)
        ordered = list(selected_rows.values())
        ordered.sort(
            key=lambda item: (
                -SOURCE_RANK[item[0]["source"]],
                -item[1],
                -int(item[0]["priority"]),
                _DescendingText(item[0]["updated_at"]),
                item[0]["normalized_key"],
                int(item[0]["id"]),
            )
        )
        return [
            self._term_from_row(row, matched_scope_rank=rank)
            for row, rank in ordered[:limit]
        ]

    def search_relevant(
        self,
        draft: str,
        context: MemoryContext | None = None,
        *,
        limit: int = 128,
    ) -> list[TermRecord]:
        self._assert_owner()
        if not isinstance(draft, str) or not draft.strip():
            return []
        candidate_ids: set[int] = set()
        normalized_draft = normalize_key(draft)
        exact_rows = self._connection.execute(
            """
            SELECT t.id, t.normalized_key AS term_key, a.normalized_key AS alias_key
            FROM terms t LEFT JOIN aliases a
              ON a.term_id=t.id AND a.confirmed=1
            WHERE t.confirmed=1 AND t.enabled=1
            """
        )
        for row in exact_rows:
            if _contains_normalized_whole(normalized_draft, row["term_key"]) or (
                row["alias_key"]
                and _contains_normalized_whole(normalized_draft, row["alias_key"])
            ):
                candidate_ids.add(int(row["id"]))
        if self.fts_available:
            tokens = _search_tokens(normalized_draft)[:32]
            if tokens:
                query = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
                try:
                    for row in self._connection.execute(
                        "SELECT DISTINCT term_id FROM term_search_fts WHERE term_search_fts MATCH ? LIMIT ?",
                        (query, min(limit * 4, 512)),
                    ):
                        candidate_ids.add(int(row[0]))
                except sqlite3.DatabaseError:
                    pass
        return self.select_terms(context, limit=limit, term_ids=candidate_ids)

    def replacement_candidates(
        self,
        context: MemoryContext | None = None,
        *,
        draft: str | None = None,
        limit: int = 512,
    ) -> list[ReplacementCandidate]:
        terms = (
            self.search_relevant(draft, context, limit=limit)
            if draft is not None
            else self.select_terms(context, limit=limit)
        )
        grouped: dict[str, list[ReplacementCandidate]] = defaultdict(list)
        for term in terms:
            for alias in term.aliases:
                if not alias.confirmed or not alias.auto_replace:
                    continue
                grouped[alias.normalized_key].append(
                    ReplacementCandidate(
                        alias.alias_text,
                        alias.normalized_key,
                        term.canonical_text,
                        term.id,
                        alias.match_type,
                        term.precedence,
                    )
                )
        winners: list[ReplacementCandidate] = []
        for normalized_alias, candidates in grouped.items():
            candidates.sort(
                key=lambda item: (
                    -item.precedence[0],
                    -item.precedence[1],
                    -item.precedence[2],
                    _DescendingText(item.precedence[3]),
                    item.canonical_text,
                    item.term_id,
                )
            )
            winner = candidates[0]
            if len(candidates) > 1:
                challenger = candidates[1]
                if challenger.precedence == winner.precedence and (
                    challenger.canonical_text != winner.canonical_text
                ):
                    continue
            winners.append(winner)
        winners.sort(key=lambda item: (-len(item.normalized_alias), item.normalized_alias, item.term_id))
        return winners

    def mark_terms_used(self, term_ids: Iterable[int]) -> None:
        ids = tuple(sorted({int(term_id) for term_id in term_ids}))
        if not ids:
            return
        now = utc_now()
        with self._write() as connection:
            connection.executemany(
                "UPDATE terms SET use_count=use_count+1, last_used_at=? WHERE id=?",
                ((now, term_id) for term_id in ids),
            )

    def add_style_preference(
        self,
        key: str,
        value: str,
        *,
        scope: Scope = Scope(),
        source: str = "user",
        confirmed: bool = False,
        enabled: bool = True,
    ) -> StylePreference:
        data = self._validate_style_data(
            {
                "key": key,
                "value": value,
                "scope": {"type": scope.scope_type, "value": scope.value},
                "source": source,
                "confirmed": confirmed,
                "enabled": enabled,
            },
            import_source=False,
        )
        with self._write() as connection:
            existing = self._find_style_duplicate_tx(
                connection,
                key=data["key"],
                scope=data["scope"],
                source=data["source"],
            )
            if existing is not None:
                raise MemoryConflictError("style preference already exists")
            cursor = connection.execute(
                """
                INSERT INTO style_preferences(
                    preference_key, preference_value, scope_type, scope_value,
                    source, confirmed, enabled, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    data["key"],
                    data["value"],
                    data["scope"].scope_type,
                    data["scope"].value,
                    data["source"],
                    int(data["enabled"]),
                    utc_now(),
                ),
            )
            style_id = int(cursor.lastrowid)
            self._log_tx(connection, "style", style_id, "created")
        return self.get_style(style_id)

    @staticmethod
    def _find_style_duplicate_tx(
        connection: sqlite3.Connection,
        *,
        key: str,
        scope: Scope,
        source: str | None,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT id, scope_type, scope_value, source
            FROM style_preferences
            WHERE preference_key=?
            """,
            (key,),
        ).fetchall()
        for row in rows:
            existing_scope = Scope(row["scope_type"], row["scope_value"])
            if (
                existing_scope.scope_type == scope.scope_type
                and existing_scope.normalized_value == scope.normalized_value
                and (source is None or row["source"] == source)
            ):
                return row
        return None

    def _validate_style_data(self, raw: Mapping[str, Any], *, import_source: bool) -> dict[str, Any]:
        key = raw.get("key")
        if not isinstance(key, str) or not _STYLE_KEY_RE.fullmatch(key):
            raise MemoryValidationError("invalid style key")
        value = _clean_display_text(
            raw.get("value"), field_name="style.value", max_length=MAX_STYLE_VALUE_LENGTH
        )
        scope_raw = raw.get("scope", {"type": "global", "value": ""})
        scopes = self._validate_scope_list([scope_raw])
        source = raw.get("source", "import" if import_source else "user")
        if source not in TERM_SOURCES:
            raise MemoryValidationError("invalid style source")
        if import_source and source == "user":
            source = "import"
        confirmed = _bool(raw.get("confirmed", False), "style.confirmed")
        if not confirmed:
            raise MemoryValidationError("styles must be explicitly confirmed")
        return {
            "key": key,
            "value": value,
            "scope": scopes[0],
            "source": source,
            "confirmed": True,
            "enabled": _bool(raw.get("enabled", True), "style.enabled"),
        }

    def get_style(self, style_id: int) -> StylePreference:
        self._assert_owner()
        row = self._connection.execute(
            "SELECT * FROM style_preferences WHERE id=?", (style_id,)
        ).fetchone()
        if row is None:
            raise KeyError(style_id)
        return StylePreference(
            int(row["id"]),
            row["preference_key"],
            row["preference_value"],
            Scope(row["scope_type"], row["scope_value"]),
            row["source"],
            bool(row["confirmed"]),
            bool(row["enabled"]),
            row["updated_at"],
        )

    def set_style_enabled(self, style_id: int, enabled: bool) -> None:
        _bool(enabled, "enabled")
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE style_preferences SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), style_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(style_id)
            self._log_tx(
                connection,
                "style",
                style_id,
                "enabled" if enabled else "disabled",
            )

    def delete_style(self, style_id: int) -> None:
        with self._write() as connection:
            cursor = connection.execute("DELETE FROM style_preferences WHERE id=?", (style_id,))
            if cursor.rowcount != 1:
                raise KeyError(style_id)
            self._log_tx(connection, "style", style_id, "deleted")

    def select_styles(self, context: MemoryContext | None = None) -> list[StylePreference]:
        self._assert_owner()
        context = context or MemoryContext()
        candidates: list[StylePreference] = []
        for row in self._connection.execute(
            "SELECT id FROM style_preferences WHERE confirmed=1 AND enabled=1"
        ):
            style = self.get_style(int(row[0]))
            if self._scope_match_rank((style.scope,), context):
                candidates.append(style)
        candidates.sort(
            key=lambda item: (
                item.key,
                -SOURCE_RANK[item.source],
                -item.scope.specificity,
                _DescendingText(item.updated_at),
                item.id,
            )
        )
        winners: dict[str, StylePreference] = {}
        for item in candidates:
            winners.setdefault(item.key, item)
        return [winners[key] for key in sorted(winners)]

    def export_profile(self) -> dict[str, Any]:
        self._assert_owner()
        packs = [
            {
                "pack_id": row["pack_id"],
                "name": row["name"],
                "version": row["version"],
                "language": row["language"],
                "source": row["source"],
                "checksum": row["checksum"],
                "enabled": bool(row["enabled"]),
            }
            for row in self._connection.execute("SELECT * FROM packs ORDER BY pack_id")
        ]
        terms = []
        for term in self.list_terms(include_disabled=True):
            terms.append(
                {
                    "canonical_text": term.canonical_text,
                    "language": term.language,
                    "kind": term.kind,
                    "priority": term.priority,
                    "source": term.source,
                    "confirmed": term.confirmed,
                    "enabled": term.enabled,
                    "aliases": [
                        {
                            "text": alias.alias_text,
                            "match_type": alias.match_type,
                            "auto_replace": alias.auto_replace,
                            "confirmed": alias.confirmed,
                        }
                        for alias in term.aliases
                    ],
                    "scopes": [
                        {"type": scope.scope_type, "value": scope.value}
                        for scope in term.scopes
                    ],
                    "pack_ids": list(term.pack_ids),
                }
            )
        styles = [
            {
                "key": row["preference_key"],
                "value": row["preference_value"],
                "scope": {"type": row["scope_type"], "value": row["scope_value"]},
                "source": row["source"],
                "confirmed": bool(row["confirmed"]),
                "enabled": bool(row["enabled"]),
            }
            for row in self._connection.execute(
                """
                SELECT * FROM style_preferences
                WHERE confirmed=1
                ORDER BY preference_key, scope_type, scope_value, id
                """
            )
        ]
        payload = {"terms": terms, "styles": styles, "packs": packs}
        return {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "exported_at": utc_now(),
            "payload": payload,
            "checksum": payload_checksum(payload),
        }

    def export_to_file(self, destination: Path) -> Path:
        self._assert_owner()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = self.export_profile()
        encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise MemoryValidationError("export exceeds the profile size limit")
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.write_text(encoded, encoding="utf-8")
        temp.replace(destination)
        return destination

    def _parse_document(self, document: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
        if isinstance(document, Mapping):
            try:
                encoded = _canonical_json(document)
            except (TypeError, ValueError, RecursionError) as exc:
                raise MemoryValidationError("document is not valid JSON data") from exc
            parsed: Any = document
        elif isinstance(document, bytes):
            encoded = document
            try:
                parsed = _json_loads_strict(document.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise MemoryValidationError("invalid UTF-8 JSON") from exc
        elif isinstance(document, str):
            encoded = document.encode("utf-8")
            try:
                parsed = _json_loads_strict(document)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise MemoryValidationError("invalid JSON") from exc
        else:
            raise MemoryValidationError("document must be JSON text, bytes, or an object")
        if len(encoded) > MAX_IMPORT_BYTES:
            raise MemoryValidationError("import exceeds the size limit")
        if not isinstance(parsed, Mapping):
            raise MemoryValidationError("import root must be an object")
        return parsed

    def import_profile(self, document: str | bytes | Mapping[str, Any]) -> ImportSummary:
        self._assert_owner()
        parsed = self._parse_document(document)
        _require_exact_keys(
            parsed,
            required={"format", "version", "exported_at", "payload", "checksum"},
            where="profile",
        )
        if parsed["format"] != PROFILE_FORMAT or parsed["version"] != PROFILE_VERSION:
            raise MemoryValidationError("unsupported memory profile")
        if not isinstance(parsed["exported_at"], str):
            raise MemoryValidationError("invalid export timestamp")
        payload = parsed["payload"]
        if not isinstance(payload, Mapping):
            raise MemoryValidationError("payload must be an object")
        if not isinstance(parsed["checksum"], str) or not _SHA256_RE.fullmatch(parsed["checksum"]):
            raise MemoryChecksumError("invalid checksum format")
        if not hmac.compare_digest(payload_checksum(payload), parsed["checksum"]):
            raise MemoryChecksumError("profile checksum mismatch")
        terms, styles, packs = self._validate_profile_payload(payload)
        added_terms = skipped_terms = added_styles = skipped_styles = added_packs = 0
        with self._write() as connection:
            for pack in packs:
                existing = connection.execute(
                    "SELECT 1 FROM packs WHERE pack_id=?", (pack["pack_id"],)
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO packs(pack_id, name, version, language, source,
                                          checksum, enabled, installed_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pack["pack_id"], pack["name"], pack["version"],
                            pack["language"], pack["source"], pack["checksum"],
                            int(pack["enabled"]), utc_now(),
                        ),
                    )
                    added_packs += 1
            for data in terms:
                term_id = self._insert_term_tx(connection, data, on_conflict="skip")
                if term_id < 0:
                    skipped_terms += 1
                else:
                    added_terms += 1
                    self._log_tx(connection, "term", term_id, "imported")
            for data in styles:
                existing = self._find_style_duplicate_tx(
                    connection,
                    key=data["key"],
                    scope=data["scope"],
                    source=None,
                )
                if existing is not None:
                    skipped_styles += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO style_preferences(
                        preference_key, preference_value, scope_type, scope_value,
                        source, confirmed, enabled, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        data["key"], data["value"], data["scope"].scope_type,
                        data["scope"].value, data["source"], int(data["enabled"]), utc_now(),
                    ),
                )
                added_styles += 1
                self._log_tx(connection, "style", int(cursor.lastrowid), "imported")
        return ImportSummary(added_terms, skipped_terms, added_styles, skipped_styles, added_packs)

    def import_file(self, source: Path) -> ImportSummary:
        source = Path(source)
        if source.stat().st_size > MAX_IMPORT_BYTES:
            raise MemoryValidationError("import exceeds the size limit")
        return self.import_profile(source.read_bytes())

    def _validate_profile_payload(
        self, payload: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        _require_exact_keys(
            payload,
            required={"terms", "styles", "packs"},
            where="profile.payload",
        )
        raw_terms = payload["terms"]
        raw_styles = payload["styles"]
        raw_packs = payload["packs"]
        for name, value, maximum in (
            ("terms", raw_terms, MAX_IMPORT_TERMS),
            ("styles", raw_styles, MAX_IMPORT_STYLES),
            ("packs", raw_packs, MAX_PACKS_PER_IMPORT),
        ):
            if not isinstance(value, list) or len(value) > maximum:
                raise MemoryValidationError(f"invalid or oversized {name} list")
        packs = [self._validate_pack_metadata(item) for item in raw_packs]
        pack_ids = {item["pack_id"] for item in packs}
        if len(pack_ids) != len(packs):
            raise MemoryValidationError("duplicate pack ids")
        terms: list[dict[str, Any]] = []
        seen_terms: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        for raw in raw_terms:
            if not isinstance(raw, Mapping):
                raise MemoryValidationError("term must be an object")
            _require_exact_keys(
                raw,
                required={
                    "canonical_text", "language", "kind", "priority", "source",
                    "confirmed", "enabled", "aliases", "scopes", "pack_ids",
                },
                where="term",
            )
            data = self._validate_term_data(raw, import_source=True)
            if not set(data["pack_ids"]) <= pack_ids:
                raise MemoryValidationError("term refers to an unknown pack")
            if data["source"] in PACK_SOURCES and not data["pack_ids"]:
                raise MemoryValidationError("pack-sourced term must refer to a pack")
            if data["source"] not in PACK_SOURCES and data["pack_ids"]:
                raise MemoryValidationError("non-pack term cannot refer to a pack")
            signature = (
                data["normalized_key"],
                data["language"],
                self._scope_signature(data["scopes"]),
            )
            if signature in seen_terms:
                raise MemoryValidationError("duplicate term in import")
            seen_terms.add(signature)
            terms.append(data)
        styles: list[dict[str, Any]] = []
        seen_styles: set[tuple[str, str, str]] = set()
        for raw in raw_styles:
            if not isinstance(raw, Mapping):
                raise MemoryValidationError("style must be an object")
            _require_exact_keys(
                raw,
                required={"key", "value", "scope", "source", "confirmed", "enabled"},
                where="style",
            )
            data = self._validate_style_data(raw, import_source=True)
            signature = (data["key"], data["scope"].scope_type, data["scope"].value)
            if signature in seen_styles:
                raise MemoryValidationError("duplicate style in import")
            seen_styles.add(signature)
            styles.append(data)
        return terms, styles, packs

    def _validate_pack_metadata(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise MemoryValidationError("pack metadata must be an object")
        _require_exact_keys(
            raw,
            required={"pack_id", "name", "version", "language", "source", "checksum", "enabled"},
            where="pack metadata",
        )
        pack_id = raw["pack_id"]
        if not isinstance(pack_id, str) or not _IDENTIFIER_RE.fullmatch(pack_id):
            raise MemoryValidationError("invalid pack id")
        name = _clean_display_text(raw["name"], field_name="pack.name", max_length=128)
        version = _clean_display_text(raw["version"], field_name="pack.version", max_length=64)
        source = raw["source"]
        if source not in PACK_SOURCES:
            raise MemoryValidationError("invalid pack source")
        checksum = raw["checksum"]
        if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
            raise MemoryValidationError("invalid pack checksum")
        return {
            "pack_id": pack_id,
            "name": name,
            "version": version,
            "language": _normalize_language(raw["language"]),
            "source": source,
            "checksum": checksum,
            "enabled": _bool(raw["enabled"], "pack.enabled"),
        }

    def install_pack(self, document: str | bytes | Mapping[str, Any]) -> ImportSummary:
        self._assert_owner()
        parsed = self._parse_document(document)
        _require_exact_keys(
            parsed,
            required={"format", "version", "pack", "checksum"},
            where="pack document",
        )
        if parsed["format"] != PACK_FORMAT or parsed["version"] != PACK_VERSION:
            raise MemoryValidationError("unsupported pack format")
        pack = parsed["pack"]
        if not isinstance(pack, Mapping):
            raise MemoryValidationError("pack must be an object")
        checksum = parsed["checksum"]
        if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
            raise MemoryChecksumError("invalid pack checksum")
        if not hmac.compare_digest(payload_checksum(pack), checksum):
            raise MemoryChecksumError("pack checksum mismatch")
        _require_exact_keys(
            pack,
            required={"pack_id", "name", "version", "language", "source", "enabled", "terms"},
            where="pack",
        )
        raw_terms = pack["terms"]
        if not isinstance(raw_terms, list) or len(raw_terms) > MAX_IMPORT_TERMS:
            raise MemoryValidationError("invalid or oversized pack terms")
        metadata = self._validate_pack_metadata(
            {
                "pack_id": pack["pack_id"],
                "name": pack["name"],
                "version": pack["version"],
                "language": pack["language"],
                "source": pack["source"],
                "checksum": checksum,
                "enabled": pack["enabled"],
            }
        )
        terms: list[dict[str, Any]] = []
        seen_terms: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        for raw in raw_terms:
            if not isinstance(raw, Mapping):
                raise MemoryValidationError("pack term must be an object")
            _require_exact_keys(
                raw,
                required={"canonical_text", "language", "kind", "priority", "confirmed", "enabled", "aliases", "scopes"},
                where="pack term",
            )
            term_raw = dict(raw)
            term_raw["source"] = metadata["source"]
            term_raw["pack_ids"] = [metadata["pack_id"]]
            data = self._validate_term_data(term_raw, import_source=False)
            signature = (
                data["normalized_key"],
                data["language"],
                self._scope_signature(data["scopes"]),
            )
            if signature in seen_terms:
                raise MemoryValidationError("duplicate term in pack")
            seen_terms.add(signature)
            terms.append(data)
        added = skipped = 0
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO packs(pack_id, name, version, language, source, checksum, enabled, installed_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pack_id) DO UPDATE SET
                    name=excluded.name, version=excluded.version,
                    language=excluded.language, source=excluded.source,
                    checksum=excluded.checksum,
                    installed_at=excluded.installed_at
                """,
                (
                    metadata["pack_id"], metadata["name"], metadata["version"],
                    metadata["language"], metadata["source"], metadata["checksum"],
                    int(metadata["enabled"]), utc_now(),
                ),
            )
            old_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT term_id FROM pack_terms WHERE pack_id=?", (metadata["pack_id"],)
                )
            ]
            connection.execute("DELETE FROM pack_terms WHERE pack_id=?", (metadata["pack_id"],))
            for old_id in old_ids:
                row = connection.execute(
                    "SELECT source FROM terms WHERE id=?", (old_id,)
                ).fetchone()
                linked = connection.execute(
                    "SELECT 1 FROM pack_terms WHERE term_id=?", (old_id,)
                ).fetchone()
                if row is not None and row["source"] in PACK_SOURCES and linked is None:
                    connection.execute("DELETE FROM terms WHERE id=?", (old_id,))
                    if self.fts_available:
                        connection.execute("DELETE FROM term_search_fts WHERE term_id=?", (old_id,))
            for data in terms:
                term_id = self._insert_term_tx(connection, data, on_conflict="skip")
                if term_id < 0:
                    skipped += 1
                    existing_id = -term_id
                    existing = connection.execute(
                        "SELECT source FROM terms WHERE id=?", (existing_id,)
                    ).fetchone()
                    if existing is not None and existing["source"] in PACK_SOURCES:
                        connection.execute(
                            "INSERT OR IGNORE INTO pack_terms(pack_id, term_id) VALUES(?, ?)",
                            (metadata["pack_id"], existing_id),
                        )
                else:
                    added += 1
            self._log_tx(connection, "pack", metadata["pack_id"], "imported")
        return ImportSummary(terms_added=added, terms_skipped=skipped, packs_added=1)

    def set_pack_enabled(self, pack_id: str, enabled: bool) -> None:
        _bool(enabled, "enabled")
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE packs SET enabled=? WHERE pack_id=?", (int(enabled), pack_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(pack_id)
            self._log_tx(connection, "pack", pack_id, "enabled" if enabled else "disabled")

    def list_packs(self) -> list[PackRecord]:
        self._assert_owner()
        rows = self._connection.execute(
            """
            SELECT pack_id, name, version, language, source, checksum,
                   enabled, installed_at
            FROM packs ORDER BY name COLLATE NOCASE, pack_id
            """
        ).fetchall()
        return [
            PackRecord(
                pack_id=str(row["pack_id"]),
                name=str(row["name"]),
                version=str(row["version"]),
                language=str(row["language"]),
                source=str(row["source"]),
                checksum=str(row["checksum"]),
                enabled=bool(row["enabled"]),
                installed_at=str(row["installed_at"]),
            )
            for row in rows
        ]

    def delete_pack(self, pack_id: str) -> None:
        """Remove a pack and only its now-orphaned pack-owned terms."""

        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM packs WHERE pack_id=?", (pack_id,)
            ).fetchone() is None:
                raise KeyError(pack_id)
            term_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT term_id FROM pack_terms WHERE pack_id=?", (pack_id,)
                )
            ]
            connection.execute("DELETE FROM packs WHERE pack_id=?", (pack_id,))
            for term_id in term_ids:
                row = connection.execute(
                    "SELECT source FROM terms WHERE id=?", (term_id,)
                ).fetchone()
                still_linked = connection.execute(
                    "SELECT 1 FROM pack_terms WHERE term_id=?", (term_id,)
                ).fetchone()
                if row is not None and row["source"] in PACK_SOURCES and still_linked is None:
                    connection.execute("DELETE FROM terms WHERE id=?", (term_id,))
                    if self.fts_available:
                        connection.execute("DELETE FROM term_search_fts WHERE term_id=?", (term_id,))
            self._log_tx(connection, "pack", pack_id, "deleted")

    @staticmethod
    def _log_tx(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: int | str,
        action: str,
    ) -> None:
        # Structural metadata only: never transcript, audio, term text, or secrets.
        connection.execute(
            """
            INSERT INTO memory_change_log(entity_type, entity_id, action, changed_at)
            VALUES(?, ?, ?, ?)
            """,
            (entity_type, str(entity_id), action, utc_now()),
        )


@dataclass(frozen=True, slots=True)
class _DescendingText:
    value: str

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _DescendingText):
            return NotImplemented
        return self.value > other.value


def _is_word_character(character: str) -> bool:
    if not character:
        return False
    return unicodedata.category(character)[0] in {"L", "N", "M"} or character == "_"


def _contains_normalized_whole(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index else ""
        after_index = index + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else ""
        if not _is_word_character(before) and not _is_word_character(after):
            return True
        start = index + 1


def _search_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))


def make_pack_document(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Create a checksummed pack envelope after the caller builds pack data."""

    return {
        "format": PACK_FORMAT,
        "version": PACK_VERSION,
        "pack": dict(pack),
        "checksum": payload_checksum(pack),
    }


def open_memory_store_resilient(
    path: Path | None = None,
    *,
    app_version: str = "0",
    enable_fts: bool = True,
) -> tuple[MemoryStore, Path | None]:
    """Preserve an unreadable database and return a clean usable store.

    Migration/permission errors are not hidden. Recovery is limited to an
    existing file that SQLite itself reports as an invalid database.
    """

    target = Path(path) if path is not None else state_dir() / "data" / "memory.sqlite3"
    try:
        return (
            MemoryStore(target, app_version=app_version, enable_fts=enable_fts),
            None,
        )
    except sqlite3.DatabaseError:
        if not target.is_file():
            raise
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        recovery = target.with_name(f"{target.name}.corrupt-{stamp}")
        target.replace(recovery)
        return (
            MemoryStore(target, app_version=app_version, enable_fts=enable_fts),
            recovery,
        )
