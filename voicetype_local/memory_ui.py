from __future__ import annotations

"""Explicit, keyboard-accessible editor for VoiceType structured memory.

Importing this module creates no Tk root and opens no window. The editor uses
only the public ``MemoryStore`` API and never handles transcripts, audio, or
secrets. Import delegates to ``MemoryStore.import_file``, whose conflict policy
adds new records and skips existing records instead of overwriting user data.
"""

import tkinter as tk
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from .memory import (
    AliasInput,
    ImportSummary,
    MAX_ALIAS_LENGTH,
    MAX_TERM_LENGTH,
    MemoryConflictError,
    MemoryStore,
    MemoryValidationError,
    PackRecord,
    Scope,
    TermRecord,
    normalize_key,
)


LANGUAGE_SUGGESTIONS = ("und", "ru", "es", "en")
SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("global", "Глобально"),
    ("language", "Для языка"),
    ("domain", "Для домена"),
    ("app", "Для приложения"),
)


__all__ = [
    "LANGUAGE_SUGGESTIONS",
    "MemoryUIController",
    "MemoryUIValidationError",
    "MemoryWindow",
    "SCOPE_OPTIONS",
    "TermFormModel",
    "TermFormValues",
    "format_import_summary",
    "open_memory_window",
]


class MemoryUIValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(next(iter(self.errors.values()), "Проверьте поля"))


@dataclass(slots=True)
class TermFormValues:
    canonical: str = ""
    alias: str = ""
    language: str = "und"
    priority: str = "0"
    scope_type: str = "global"
    scope_value: str = ""


def _has_forbidden_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


class TermFormModel:
    """Display-free validation for one explicitly confirmed term."""

    _FIELDS = frozenset(TermFormValues.__dataclass_fields__)

    def __init__(self, values: TermFormValues | None = None) -> None:
        self.values = values or TermFormValues()

    def update(self, **changes: Any) -> None:
        unknown = set(changes) - self._FIELDS
        if unknown:
            raise KeyError(f"Unknown term fields: {', '.join(sorted(unknown))}")
        for name, value in changes.items():
            setattr(self.values, name, value)

    def _parsed_priority(self) -> int | None:
        value = self.values.priority
        if isinstance(value, bool):
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if -1000 <= parsed <= 1000 else None

    def validation_errors(self) -> dict[str, str]:
        values = self.values
        errors: dict[str, str] = {}
        canonical = str(values.canonical).strip()
        alias = str(values.alias).strip()
        language = str(values.language).strip() or "und"
        scope_type = str(values.scope_type)
        scope_value = str(values.scope_value).strip()

        if not canonical:
            errors["canonical"] = "Введите подтверждённое написание термина."
        elif len(canonical) > MAX_TERM_LENGTH or _has_forbidden_control(canonical):
            errors["canonical"] = "Термин слишком длинный или содержит недопустимый символ."

        if alias:
            if len(alias) > MAX_ALIAS_LENGTH or _has_forbidden_control(alias):
                errors["alias"] = "Алиас слишком длинный или содержит недопустимый символ."
            elif canonical and normalize_key(alias) == normalize_key(canonical):
                errors["alias"] = "Алиас должен отличаться от подтверждённого термина."

        if language != "und":
            try:
                Scope("language", language)
            except MemoryValidationError:
                errors["language"] = "Укажите код языка, например ru, es или en."

        if self._parsed_priority() is None:
            errors["priority"] = "Приоритет должен быть целым числом от -1000 до 1000."

        if scope_type not in {item[0] for item in SCOPE_OPTIONS}:
            errors["scope_type"] = "Выберите область действия из списка."
        else:
            try:
                Scope(scope_type, scope_value)
            except MemoryValidationError:
                if scope_type == "global":
                    errors["scope_value"] = "Для глобальной области значение должно быть пустым."
                elif scope_type == "language":
                    errors["scope_value"] = "Для области языка укажите конкретный код языка."
                elif scope_type == "domain":
                    errors["scope_value"] = "Укажите домен, например example.com."
                else:
                    errors["scope_value"] = "Укажите идентификатор приложения."
        return errors

    def store_kwargs(self) -> dict[str, object]:
        errors = self.validation_errors()
        if errors:
            raise MemoryUIValidationError(errors)

        values = self.values
        canonical = str(values.canonical).strip()
        alias = str(values.alias).strip()
        language = str(values.language).strip() or "und"
        scope_type = str(values.scope_type)
        scope_value = str(values.scope_value).strip()
        aliases: tuple[AliasInput, ...] = (
            (AliasInput(alias, confirmed=True),) if alias else ()
        )
        return {
            "canonical_text": canonical,
            "language": language,
            "kind": "term",
            "priority": self._parsed_priority(),
            "source": "user",
            "confirmed": True,
            "enabled": True,
            "aliases": aliases,
            "scopes": (Scope(scope_type, scope_value),),
        }


class MemoryUIController:
    """Small synchronous adapter around the owner-thread ``MemoryStore``."""

    def __init__(self, store: MemoryStore, *, list_limit: int = 1000) -> None:
        self.store = store
        self.list_limit = max(1, int(list_limit))

    @staticmethod
    def _searchable_text(term: TermRecord) -> str:
        parts = [term.canonical_text, term.language]
        parts.extend(alias.alias_text for alias in term.aliases)
        parts.extend(f"{scope.scope_type} {scope.value}" for scope in term.scopes)
        return normalize_key(" ".join(parts))

    def list_terms(self, query: str = "") -> list[TermRecord]:
        terms = self.store.list_terms(include_disabled=True)
        needle = normalize_key(str(query).strip()) if str(query).strip() else ""
        if needle:
            terms = [term for term in terms if needle in self._searchable_text(term)]
        return terms[: self.list_limit]

    def list_packs(self) -> list[PackRecord]:
        return self.store.list_packs()

    def add_term(self, form: TermFormModel) -> TermRecord:
        return self.store.add_term(**form.store_kwargs())  # type: ignore[arg-type]

    def set_enabled(self, term_id: int, enabled: bool) -> None:
        self.store.set_term_enabled(int(term_id), bool(enabled))

    def toggle_enabled(self, term_id: int) -> bool:
        term = self.store.get_term(int(term_id))
        if term is None:
            raise KeyError(term_id)
        enabled = not term.enabled
        self.store.set_term_enabled(term.id, enabled)
        return enabled

    def toggle_pack_enabled(self, pack_id: str) -> bool:
        pack = next(
            (item for item in self.store.list_packs() if item.pack_id == pack_id),
            None,
        )
        if pack is None:
            raise KeyError(pack_id)
        enabled = not pack.enabled
        self.store.set_pack_enabled(pack.pack_id, enabled)
        return enabled

    def delete_term(self, term_id: int, *, confirmed: bool = False) -> None:
        if confirmed is not True:
            raise MemoryUIValidationError(
                {"confirmation": "Подтвердите удаление выбранного термина."}
            )
        self.store.delete_term(int(term_id))

    def import_file(self, source: Path | str) -> ImportSummary:
        # MemoryStore import is additive and skips conflicts; it never updates
        # an existing local user term.
        return self.store.import_file(Path(source))

    def export_file(self, destination: Path | str) -> Path:
        return self.store.export_to_file(Path(destination))


def format_import_summary(summary: ImportSummary) -> str:
    """Return counts only; never include imported content or sensitive data."""

    added = int(summary.terms_added) + int(summary.styles_added) + int(summary.packs_added)
    skipped = int(summary.terms_skipped) + int(summary.styles_skipped)
    return f"Импорт завершён: добавлено {added}, пропущено существующих {skipped}."


def _scope_label(term: TermRecord) -> str:
    labels = dict(SCOPE_OPTIONS)
    return ", ".join(
        labels.get(scope.scope_type, scope.scope_type)
        + (f": {scope.value}" if scope.value else "")
        for scope in term.scopes
    )


def _scope_value(label: str) -> str:
    return next((value for value, text in SCOPE_OPTIONS if text == label), "global")


def _scope_display(value: str) -> str:
    return next((text for item, text in SCOPE_OPTIONS if item == value), SCOPE_OPTIONS[0][1])


class MemoryWindow:
    """Explicit large-font term manager with no background data access."""

    def __init__(
        self,
        parent: tk.Misc,
        store: MemoryStore,
        *,
        import_callback: Callable[[], ImportSummary] | None = None,
        export_callback: Callable[[], Path | str | None] | None = None,
        on_close: Callable[[], object] | None = None,
    ) -> None:
        self.controller = MemoryUIController(store)
        self._import_callback = import_callback
        self._export_callback = export_callback
        self._on_close_callback = on_close
        self._closed = False
        self._terms: dict[int, TermRecord] = {}
        self._packs: dict[str, PackRecord] = {}
        self._pending_delete_id: int | None = None
        self.form = TermFormModel()

        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("VoiceType Local — Память терминов")
        self.window.transient(parent.winfo_toplevel())
        self.window.geometry("1220x760")
        self.window.minsize(980, 660)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", self._escape_event)

        self._configure_styles()
        self._create_variables()
        self._field_widgets: dict[str, tk.Misc] = {}
        self._build()
        self.refresh()

        self.window.update_idletasks()
        self.window.deiconify()
        self.window.grab_set()
        self.window.after_idle(self._search_entry.focus_set)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        style.configure("VoiceType.Memory.TFrame", background="#F8FAFC")
        style.configure(
            "VoiceType.Memory.Title.TLabel",
            font=("Segoe UI", 20, "bold"),
            background="#F8FAFC",
            foreground="#0F172A",
        )
        style.configure(
            "VoiceType.Memory.TLabel",
            font=("Segoe UI", 13),
            background="#F8FAFC",
            foreground="#0F172A",
        )
        style.configure(
            "VoiceType.Memory.Status.TLabel",
            font=("Segoe UI", 12, "bold"),
            background="#F8FAFC",
            foreground="#334155",
        )
        style.configure(
            "VoiceType.Memory.TLabelframe.Label", font=("Segoe UI", 15, "bold")
        )
        style.configure("VoiceType.Memory.TButton", font=("Segoe UI", 13), padding=(12, 8))
        style.configure(
            "VoiceType.Memory.Treeview", font=("Segoe UI", 12), rowheight=32
        )
        style.configure(
            "VoiceType.Memory.Treeview.Heading", font=("Segoe UI", 12, "bold")
        )

    def _create_variables(self) -> None:
        self._search_var = tk.StringVar(self.window, "")
        self._canonical_var = tk.StringVar(self.window, "")
        self._alias_var = tk.StringVar(self.window, "")
        self._language_var = tk.StringVar(self.window, "und")
        self._priority_var = tk.StringVar(self.window, "0")
        self._scope_type_var = tk.StringVar(self.window, _scope_display("global"))
        self._scope_value_var = tk.StringVar(self.window, "")
        self._status_var = tk.StringVar(self.window, "")
        self._confirmation_var = tk.StringVar(self.window, "")

    def _keyboard_button(
        self, parent: tk.Misc, *, text: str, command: Callable[[], object]
    ) -> ttk.Button:
        button = ttk.Button(
            parent,
            text=text,
            command=command,
            takefocus=True,
            style="VoiceType.Memory.TButton",
        )

        def invoke(_event: tk.Event[tk.Misc]) -> str:
            button.invoke()
            return "break"

        button.bind("<Return>", invoke)
        return button

    def _build(self) -> None:
        root = ttk.Frame(self.window, padding=18, style="VoiceType.Memory.TFrame")
        root.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(2, weight=1)

        ttk.Label(
            root,
            text="Память подтверждённых терминов",
            style="VoiceType.Memory.Title.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            root,
            text=(
                "Tab — переход · стрелки — выбор термина · Enter — действие · "
                "Esc — отмена / закрытие"
            ),
            style="VoiceType.Memory.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))

        listing = ttk.LabelFrame(
            root, text="Термины", padding=10, style="VoiceType.Memory.TLabelframe"
        )
        editor = ttk.LabelFrame(
            root,
            text="Добавить подтверждённый термин",
            padding=10,
            style="VoiceType.Memory.TLabelframe",
        )
        listing.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        editor.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(1, weight=1)
        editor.columnconfigure(0, weight=1)

        search_bar = ttk.Frame(listing)
        search_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_bar.columnconfigure(0, weight=1)
        self._search_entry = ttk.Entry(
            search_bar,
            textvariable=self._search_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        self._search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._search_entry.bind("<Return>", self._search_event)
        self._keyboard_button(
            search_bar, text="Найти", command=self.refresh
        ).grid(row=0, column=1)

        columns = ("canonical", "alias", "language", "scope", "priority", "state")
        self._tree = ttk.Treeview(
            listing,
            columns=columns,
            show="headings",
            selectmode="browse",
            takefocus=True,
            style="VoiceType.Memory.Treeview",
        )
        headings = {
            "canonical": "Термин",
            "alias": "Алиасы",
            "language": "Язык",
            "scope": "Область",
            "priority": "Приоритет",
            "state": "Статус",
        }
        widths = {
            "canonical": 170,
            "alias": 150,
            "language": 70,
            "scope": 165,
            "priority": 85,
            "state": 95,
        }
        for column in columns:
            self._tree.heading(column, text=headings[column])
            self._tree.column(column, width=widths[column], minwidth=60, stretch=True)
        tree_scroll = ttk.Scrollbar(listing, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=tree_scroll.set)
        self._tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self._tree.bind("<Return>", self._toggle_event)

        actions = ttk.Frame(listing)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        action_specs = (
            ("Включить / выключить", self.toggle_selected),
            ("Удалить…", self.request_delete),
            ("Импорт…", self.import_terms),
            ("Экспорт…", self.export_terms),
        )
        for index, (text, command) in enumerate(action_specs):
            self._keyboard_button(actions, text=text, command=command).grid(
                row=0, column=index, padx=(0, 7)
            )

        packs = ttk.LabelFrame(
            listing,
            text="Установленные тематические наборы",
            padding=8,
            style="VoiceType.Memory.TLabelframe",
        )
        packs.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        packs.columnconfigure(0, weight=1)
        pack_columns = ("name", "version", "language", "state")
        self._pack_tree = ttk.Treeview(
            packs,
            columns=pack_columns,
            show="headings",
            selectmode="browse",
            height=4,
            takefocus=True,
            style="VoiceType.Memory.Treeview",
        )
        pack_headings = {
            "name": "Набор",
            "version": "Версия",
            "language": "Язык",
            "state": "Статус",
        }
        pack_widths = {"name": 270, "version": 90, "language": 80, "state": 100}
        for column in pack_columns:
            self._pack_tree.heading(column, text=pack_headings[column])
            self._pack_tree.column(
                column, width=pack_widths[column], minwidth=60, stretch=True
            )
        self._pack_tree.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._pack_tree.bind("<Return>", self._toggle_pack_event)
        self._keyboard_button(
            packs,
            text="Включить / выключить набор",
            command=self.toggle_selected_pack,
        ).grid(row=0, column=1, sticky="ns")

        self._canonical_entry = self._entry(
            editor, 0, "Подтверждённое написание *", self._canonical_var
        )
        self._alias_entry = self._entry(
            editor, 2, "Алиас (необязательно)", self._alias_var
        )
        ttk.Label(editor, text="Язык", style="VoiceType.Memory.TLabel").grid(
            row=4, column=0, sticky="w", padx=10, pady=(8, 3)
        )
        self._language_combo = ttk.Combobox(
            editor,
            textvariable=self._language_var,
            values=LANGUAGE_SUGGESTIONS,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        self._language_combo.grid(row=5, column=0, sticky="ew", padx=10)

        ttk.Label(editor, text="Приоритет (-1000…1000)", style="VoiceType.Memory.TLabel").grid(
            row=6, column=0, sticky="w", padx=10, pady=(8, 3)
        )
        self._priority_spin = ttk.Spinbox(
            editor,
            from_=-1000,
            to=1000,
            increment=10,
            textvariable=self._priority_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        self._priority_spin.grid(row=7, column=0, sticky="ew", padx=10)

        ttk.Label(editor, text="Область действия", style="VoiceType.Memory.TLabel").grid(
            row=8, column=0, sticky="w", padx=10, pady=(8, 3)
        )
        self._scope_combo = ttk.Combobox(
            editor,
            textvariable=self._scope_type_var,
            values=tuple(text for _, text in SCOPE_OPTIONS),
            state="readonly",
            font=("Segoe UI", 14),
            takefocus=True,
        )
        self._scope_combo.grid(row=9, column=0, sticky="ew", padx=10)
        self._scope_combo.bind("<<ComboboxSelected>>", self._scope_changed)

        self._scope_value_entry = self._entry(
            editor,
            10,
            "Значение области (язык / домен / приложение)",
            self._scope_value_var,
        )
        self._scope_value_entry.configure(state="disabled")

        add_button = self._keyboard_button(
            editor, text="Добавить подтверждённый термин (Enter)", command=self.add_term
        )
        add_button.grid(row=12, column=0, sticky="ew", padx=10, pady=(16, 8))

        self._form_widgets = {
            self._canonical_entry,
            self._alias_entry,
            self._language_combo,
            self._priority_spin,
            self._scope_combo,
            self._scope_value_entry,
        }
        for widget in self._form_widgets:
            widget.bind("<Return>", self._add_event)
        self._field_widgets.update(
            {
                "canonical": self._canonical_entry,
                "alias": self._alias_entry,
                "language": self._language_combo,
                "priority": self._priority_spin,
                "scope_type": self._scope_combo,
                "scope_value": self._scope_value_entry,
            }
        )

        self._confirmation = ttk.Frame(root, padding=8)
        ttk.Label(
            self._confirmation,
            textvariable=self._confirmation_var,
            font=("Segoe UI", 13, "bold"),
            foreground="#B91C1C",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._confirm_delete_button = self._keyboard_button(
            self._confirmation, text="Да, удалить (Enter)", command=self.confirm_delete
        )
        self._confirm_delete_button.grid(row=0, column=1, padx=(0, 8))
        self._keyboard_button(
            self._confirmation, text="Нет (Esc)", command=self.cancel_delete
        ).grid(row=0, column=2)
        self._confirmation.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._confirmation.grid_remove()

        ttk.Label(
            root,
            textvariable=self._status_var,
            style="VoiceType.Memory.Status.TLabel",
            wraplength=980,
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 5))
        self._keyboard_button(root, text="Закрыть (Esc)", command=self.close).grid(
            row=5, column=0, columnspan=2, sticky="e"
        )

    def _entry(
        self, parent: tk.Misc, row: int, label: str, variable: tk.StringVar
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="VoiceType.Memory.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=(8, 3)
        )
        entry = ttk.Entry(
            parent,
            textvariable=variable,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        entry.grid(row=row + 1, column=0, sticky="ew", padx=10)
        return entry

    def _selected_term(self) -> TermRecord | None:
        selection = self._tree.selection()
        if not selection:
            self._status_var.set("Сначала выберите термин стрелками в списке.")
            self._tree.focus_set()
            return None
        try:
            return self._terms.get(int(selection[0]))
        except (TypeError, ValueError):
            return None

    def refresh(self, *, announce: bool = True, select_id: int | None = None) -> None:
        if self._closed:
            return
        previous = select_id
        if previous is None:
            selected = self._tree.selection()
            if selected:
                try:
                    previous = int(selected[0])
                except ValueError:
                    previous = None
        try:
            terms = self.controller.list_terms(self._search_var.get())
        except Exception:
            self._status_var.set("Не удалось прочитать память терминов.")
            return
        self._terms = {term.id: term for term in terms}
        self._tree.delete(*self._tree.get_children())
        for term in terms:
            aliases = ", ".join(alias.alias_text for alias in term.aliases)
            item = str(term.id)
            self._tree.insert(
                "",
                "end",
                iid=item,
                values=(
                    term.canonical_text,
                    aliases,
                    term.language,
                    _scope_label(term),
                    term.priority,
                    "Включён" if term.enabled else "Выключен",
                ),
                tags=("enabled" if term.enabled else "disabled",),
            )
        self._tree.tag_configure("disabled", foreground="#64748B")
        if previous is not None and previous in self._terms:
            item = str(previous)
            self._tree.selection_set(item)
            self._tree.focus(item)
            self._tree.see(item)
        pack_count = self.refresh_packs()
        if announce:
            self._status_var.set(
                f"Терминов в списке: {len(terms)}. Тематических наборов: {pack_count}."
            )

    def refresh_packs(self, *, select_id: str | None = None) -> int:
        previous = select_id
        if previous is None:
            selected = self._pack_tree.selection()
            if selected:
                previous = selected[0]
        try:
            packs = self.controller.list_packs()
        except Exception:
            self._packs = {}
            self._pack_tree.delete(*self._pack_tree.get_children())
            return 0
        self._packs = {pack.pack_id: pack for pack in packs}
        self._pack_tree.delete(*self._pack_tree.get_children())
        for pack in packs:
            self._pack_tree.insert(
                "",
                "end",
                iid=pack.pack_id,
                values=(
                    pack.name,
                    pack.version,
                    pack.language,
                    "Включён" if pack.enabled else "Выключен",
                ),
                tags=("enabled" if pack.enabled else "disabled",),
            )
        self._pack_tree.tag_configure("disabled", foreground="#64748B")
        if previous is not None and previous in self._packs:
            self._pack_tree.selection_set(previous)
            self._pack_tree.focus(previous)
            self._pack_tree.see(previous)
        return len(packs)

    def _sync_form(self) -> None:
        self.form.update(
            canonical=self._canonical_var.get(),
            alias=self._alias_var.get(),
            language=self._language_var.get(),
            priority=self._priority_var.get(),
            scope_type=_scope_value(self._scope_type_var.get()),
            scope_value=self._scope_value_var.get(),
        )

    def add_term(self) -> None:
        self._sync_form()
        try:
            term = self.controller.add_term(self.form)
        except MemoryUIValidationError as exc:
            field, message = next(iter(exc.errors.items()))
            self._status_var.set(message)
            widget = self._field_widgets.get(field)
            if widget is not None:
                widget.focus_set()
            return
        except MemoryConflictError:
            self._status_var.set(
                "Такой подтверждённый термин уже существует в этой области. Данные не изменены."
            )
            self._canonical_entry.focus_set()
            return
        except (MemoryValidationError, ValueError):
            self._status_var.set("Термин не добавлен: проверьте заполненные поля.")
            return
        except Exception:
            self._status_var.set("Не удалось добавить термин. Существующие данные не изменены.")
            return

        self._canonical_var.set("")
        self._alias_var.set("")
        self.refresh(announce=False, select_id=term.id)
        self._status_var.set("Подтверждённый термин добавлен.")
        self._canonical_entry.focus_set()

    def toggle_selected(self) -> None:
        term = self._selected_term()
        if term is None:
            return
        try:
            enabled = self.controller.toggle_enabled(term.id)
        except Exception:
            self._status_var.set("Не удалось изменить состояние термина.")
            return
        self.refresh(announce=False, select_id=term.id)
        self._status_var.set("Термин включён." if enabled else "Термин выключен.")
        self._tree.focus_set()

    def toggle_selected_pack(self) -> None:
        selection = self._pack_tree.selection()
        if not selection:
            self._status_var.set("Сначала выберите тематический набор стрелками.")
            self._pack_tree.focus_set()
            return
        pack_id = selection[0]
        try:
            enabled = self.controller.toggle_pack_enabled(pack_id)
        except Exception:
            self._status_var.set("Не удалось изменить состояние тематического набора.")
            return
        self.refresh_packs(select_id=pack_id)
        self._status_var.set(
            "Тематический набор включён."
            if enabled
            else "Тематический набор выключен."
        )
        self._pack_tree.focus_set()

    def request_delete(self) -> None:
        term = self._selected_term()
        if term is None:
            return
        self._pending_delete_id = term.id
        display = term.canonical_text
        if len(display) > 64:
            display = display[:61] + "…"
        self._confirmation_var.set(f"Удалить «{display}»?")
        self._confirmation.grid()
        self._confirm_delete_button.focus_set()

    def confirm_delete(self) -> None:
        term_id = self._pending_delete_id
        if term_id is None:
            return
        try:
            self.controller.delete_term(term_id, confirmed=True)
        except Exception:
            self._status_var.set("Не удалось удалить термин. Данные сохранены.")
            return
        self._pending_delete_id = None
        self._confirmation.grid_remove()
        self.refresh(announce=False)
        self._status_var.set("Термин удалён.")
        self._tree.focus_set()

    def cancel_delete(self) -> None:
        self._pending_delete_id = None
        self._confirmation.grid_remove()
        self._status_var.set("Удаление отменено.")
        self._tree.focus_set()

    def import_terms(self) -> None:
        try:
            if self._import_callback is not None:
                summary = self._import_callback()
            else:
                selected = filedialog.askopenfilename(
                    parent=self.window,
                    title="Импорт памяти VoiceType",
                    filetypes=(("Профиль VoiceType JSON", "*.json"), ("Все файлы", "*.*")),
                )
                if not selected:
                    self._status_var.set("Импорт отменён.")
                    return
                summary = self.controller.import_file(selected)
        except Exception:
            self._status_var.set(
                "Импорт не выполнен: файл не прошёл проверку. Существующие данные сохранены."
            )
            return
        self.refresh(announce=False)
        self._status_var.set(format_import_summary(summary))

    def export_terms(self) -> None:
        try:
            if self._export_callback is not None:
                result = self._export_callback()
            else:
                selected = filedialog.asksaveasfilename(
                    parent=self.window,
                    title="Экспорт памяти VoiceType",
                    defaultextension=".json",
                    filetypes=(("Профиль VoiceType JSON", "*.json"),),
                    confirmoverwrite=True,
                )
                if not selected:
                    self._status_var.set("Экспорт отменён.")
                    return
                result = self.controller.export_file(selected)
        except Exception:
            self._status_var.set("Не удалось экспортировать профиль памяти.")
            return
        filename = Path(result).name if result else ""
        self._status_var.set(
            f"Экспорт завершён: {filename}." if filename else "Экспорт завершён."
        )

    def _scope_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        scope_type = _scope_value(self._scope_type_var.get())
        if scope_type == "global":
            self._scope_value_var.set("")
            self._scope_value_entry.configure(state="disabled")
            return
        self._scope_value_entry.configure(state="normal")
        if scope_type == "language" and not self._scope_value_var.get().strip():
            language = self._language_var.get().strip()
            if language not in {"", "und"}:
                self._scope_value_var.set(language)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        if self._on_close_callback is not None:
            try:
                self._on_close_callback()
            except Exception:
                pass
        self.window.destroy()

    def _search_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.refresh()
        self._tree.focus_set()
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[0])
            self._tree.focus(children[0])
        return "break"

    def _add_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.add_term()
        return "break"

    def _toggle_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.toggle_selected()
        return "break"

    def _toggle_pack_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.toggle_selected_pack()
        return "break"

    def _escape_event(self, _event: tk.Event[tk.Misc]) -> str:
        if self._pending_delete_id is not None:
            self.cancel_delete()
        else:
            self.close()
        return "break"


def open_memory_window(
    parent: tk.Misc,
    store: MemoryStore,
    *,
    import_callback: Callable[[], ImportSummary] | None = None,
    export_callback: Callable[[], Path | str | None] | None = None,
    on_close: Callable[[], object] | None = None,
) -> MemoryWindow:
    """Explicitly open the editor; importing this module has no UI effect."""

    return MemoryWindow(
        parent,
        store,
        import_callback=import_callback,
        export_callback=export_callback,
        on_close=on_close,
    )
