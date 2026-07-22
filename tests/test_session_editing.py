from __future__ import annotations

import pytest

from voicetype_local.session_editing import (
    SessionEditAction,
    SessionEditCommandParser,
    SessionEditPlanError,
    SessionEditRequest,
    plan_session_edit,
)


@pytest.mark.parametrize(
    ("text", "mode", "language", "action"),
    (
        ("команда удали последнее слово", "mixed", "ru", SessionEditAction.DELETE_LAST_WORD),
        ("delete the last sentence", "commands", "en", SessionEditAction.DELETE_LAST_SENTENCE),
        ("comando borra el último dictado", "mixed", "es", SessionEditAction.DELETE_LAST_DICTATION),
        ("command undo last voice edit", "mixed", "en", SessionEditAction.RESTORE_LAST_EDIT),
    ),
)
def test_strict_multilingual_session_edit_commands(
    text: str, mode: str, language: str, action: SessionEditAction
) -> None:
    result = SessionEditCommandParser().parse(
        text, mode=mode, language=language
    )

    assert result.recognized
    assert result.request is not None
    assert result.request.action is action


@pytest.mark.parametrize(
    "separator",
    (". ", "... ", "! ", "? ", "… ", ": ", " — "),
)
def test_mixed_mode_accepts_whisper_punctuation_after_command_prefix(
    separator: str,
) -> None:
    result = SessionEditCommandParser().parse(
        f"Команда{separator}удали последнее предложение.",
        mode="mixed",
        language="auto",
    )

    assert result.recognized
    assert result.request is not None
    assert result.request.action is SessionEditAction.DELETE_LAST_SENTENCE


@pytest.mark.parametrize(
    ("text", "action", "payload"),
    (
        (
            "КОМАНДА! Пожалуйста, удалите последнее предложение?",
            SessionEditAction.DELETE_LAST_SENTENCE,
            None,
        ),
        (
            "COMMAND: Please, restore the last voice edit.",
            SessionEditAction.RESTORE_LAST_EDIT,
            None,
        ),
        (
            "COMANDO… Por favor, elimine la última oración.",
            SessionEditAction.DELETE_LAST_SENTENCE,
            None,
        ),
        (
            "Команда, замените: последнее слово на: готово.",
            SessionEditAction.REPLACE_LAST_WORD,
            "готово.",
        ),
        (
            "COMMAND—Replace, the last sentence with: Done!",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            "Done!",
        ),
        (
            "COMANDO: Reemplace, la última frase por: Listo.",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            "Listo.",
        ),
    ),
)
def test_closed_asr_case_punctuation_and_inflection_variants(
    text: str,
    action: SessionEditAction,
    payload: str | None,
) -> None:
    result = SessionEditCommandParser().parse(
        text,
        mode="mixed",
        language="auto",
    )

    assert result.request is not None
    assert result.request.action is action
    assert result.request.new_text == payload


@pytest.mark.parametrize(
    ("text", "language", "action"),
    (
        ("удалить последнее предложение", "ru", SessionEditAction.DELETE_LAST_SENTENCE),
        ("заменить последнее слово на готово", "ru", SessionEditAction.REPLACE_LAST_WORD),
        ("remove the last word", "en", SessionEditAction.DELETE_LAST_WORD),
        ("borrar la última frase", "es", SessionEditAction.DELETE_LAST_SENTENCE),
        ("reemplazar la última palabra por listo", "es", SessionEditAction.REPLACE_LAST_WORD),
    ),
)
def test_common_whisper_infinitive_variants_are_supported(
    text: str,
    language: str,
    action: SessionEditAction,
) -> None:
    result = SessionEditCommandParser().parse(
        text,
        mode="commands",
        language=language,
    )

    assert result.request is not None
    assert result.request.action is action


@pytest.mark.parametrize(
    ("body", "action"),
    (
        ("убери последнее слово", SessionEditAction.DELETE_LAST_WORD),
        ("убрать последнее слово", SessionEditAction.DELETE_LAST_WORD),
        ("убери последнее предложение", SessionEditAction.DELETE_LAST_SENTENCE),
        ("убрать последнее предложение", SessionEditAction.DELETE_LAST_SENTENCE),
        ("убери последнюю фразу", SessionEditAction.DELETE_LAST_SENTENCE),
        ("убрать последнюю фразу", SessionEditAction.DELETE_LAST_SENTENCE),
        ("убери последнюю диктовку", SessionEditAction.DELETE_LAST_DICTATION),
        ("убрать последнюю диктовку", SessionEditAction.DELETE_LAST_DICTATION),
        ("убери последний текст", SessionEditAction.DELETE_LAST_DICTATION),
        ("убрать последний текст", SessionEditAction.DELETE_LAST_DICTATION),
        ("восстанови последнее исправление", SessionEditAction.RESTORE_LAST_EDIT),
        ("восстановить последнее исправление", SessionEditAction.RESTORE_LAST_EDIT),
        ("восстанови последнее изменение", SessionEditAction.RESTORE_LAST_EDIT),
        ("восстановить последнее изменение", SessionEditAction.RESTORE_LAST_EDIT),
        ("верни последнее изменение", SessionEditAction.RESTORE_LAST_EDIT),
        ("вернуть последнюю правку", SessionEditAction.RESTORE_LAST_EDIT),
        ("отмени последнее изменение", SessionEditAction.RESTORE_LAST_EDIT),
        ("отменить последнее изменение", SessionEditAction.RESTORE_LAST_EDIT),
        ("отмени последнюю правку", SessionEditAction.RESTORE_LAST_EDIT),
        ("отменить последнюю правку", SessionEditAction.RESTORE_LAST_EDIT),
    ),
)
def test_safe_russian_edit_aliases_require_exact_grammar_in_mixed_mode(
    body: str,
    action: SessionEditAction,
) -> None:
    result = SessionEditCommandParser().parse(
        f"команда {body}", mode="mixed", language="ru"
    )

    assert result.recognized
    assert result.request is not None
    assert result.request.action is action


@pytest.mark.parametrize("separator", (" ", ", ", ". "))
def test_one_polite_word_is_allowed_immediately_after_explicit_russian_prefix(
    separator: str,
) -> None:
    result = SessionEditCommandParser().parse(
        f"команда пожалуйста{separator}убери последнее слово",
        mode="mixed",
        language="auto",
    )

    assert result.request is not None
    assert result.request.action is SessionEditAction.DELETE_LAST_WORD


@pytest.mark.parametrize(
    ("body", "action", "payload"),
    (
        (
            "поменяй последнее слово на VoiceType!",
            SessionEditAction.REPLACE_LAST_WORD,
            "VoiceType!",
        ),
        (
            "поменять последнее предложение на Готово,  Андрей!",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            "Готово,  Андрей!",
        ),
        (
            "поменять последнее слово на «VoiceType Local»",
            SessionEditAction.REPLACE_LAST_WORD,
            "«VoiceType Local»",
        ),
        (
            "поменяй последнее предложение на Всё ГОТОВО?!",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            "Всё ГОТОВО?!",
        ),
        (
            "поменяй: последнее слово на: VoiceType!",
            SessionEditAction.REPLACE_LAST_WORD,
            "VoiceType!",
        ),
        (
            "поменяй последнее предложение на: «Пожалуйста, готово!»",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            "«Пожалуйста, готово!»",
        ),
    ),
)
def test_russian_change_alias_preserves_replacement_payload_verbatim(
    body: str,
    action: SessionEditAction,
    payload: str,
) -> None:
    result = SessionEditCommandParser().parse(
        f"команда {body}", mode="mixed", language="ru"
    )

    assert result.request is not None
    assert result.request.action is action
    assert result.request.new_text == payload


def test_mixed_mode_never_reinterprets_unprefixed_editing_words() -> None:
    parser = SessionEditCommandParser()

    mixed = parser.parse(
        "замени масло на синтетическое", mode="mixed", language="ru"
    )
    dictation = parser.parse(
        "команда удали последнее слово", mode="dictation", language="ru"
    )

    assert not mixed.recognized
    assert not dictation.recognized


@pytest.mark.parametrize(
    "text",
    (
        "убери последнее слово",
        "поменяй последнее предложение на готово",
        "пожалуйста убери последнее слово",
        "я сказал команда убери последнее слово",
        "командовать убрать последнее слово",
        "команда пожалуйста пожалуйста убери последнее слово",
    ),
)
def test_new_russian_aliases_do_not_widen_mixed_mode_prefix_or_politeness(
    text: str,
) -> None:
    result = SessionEditCommandParser().parse(text, mode="mixed", language="ru")

    assert not result.recognized
    assert result.request is None


@pytest.mark.parametrize(
    "text",
    (
        "please delete the last word",
        "por favor borre la última palabra",
        "command please please delete the last word",
        "comando por favor por favor borre la última palabra",
        "command delete approximately the last word",
        "comando elimina algo parecido a la última frase",
    ),
)
def test_asr_tolerance_remains_prefixed_anchored_and_non_fuzzy(text: str) -> None:
    result = SessionEditCommandParser().parse(text, mode="mixed", language="auto")

    assert result.request is None
    assert not result.recognized or result.reason_code == "invalid_edit_command"


@pytest.mark.parametrize(
    "text",
    (
        "команда удали всё",
        "команда пожалуйста удали всё",
        "команда убери последнее слово и открой Chrome",
        "команда поменяй последнее предложение",
        "команда восстанови всё",
    ),
)
def test_dangerous_chained_or_incomplete_russian_edits_never_create_request(
    text: str,
) -> None:
    result = SessionEditCommandParser().parse(text, mode="mixed", language="ru")

    assert result.request is None
    assert not result.recognized or result.reason_code == "invalid_edit_command"


@pytest.mark.parametrize(
    ("text", "mode", "language", "action", "old", "new"),
    (
        (
            "команда замени Андрей на Андрея",
            "mixed",
            "ru",
            SessionEditAction.REPLACE_UNIQUE,
            "Андрей",
            "Андрея",
        ),
        (
            "replace last sentence with Everything is ready",
            "commands",
            "en",
            SessionEditAction.REPLACE_LAST_SENTENCE,
            None,
            "Everything is ready",
        ),
        (
            "reemplaza en el último texto prueba por resultado",
            "commands",
            "es",
            SessionEditAction.REPLACE_UNIQUE,
            "prueba",
            "resultado",
        ),
    ),
)
def test_replacement_arguments_are_typed_and_bounded(
    text: str,
    mode: str,
    language: str,
    action: SessionEditAction,
    old: str | None,
    new: str,
) -> None:
    result = SessionEditCommandParser().parse(
        text, mode=mode, language=language
    )

    assert result.request is not None
    assert result.request.action is action
    assert result.request.old_text == old
    assert result.request.new_text == new


def test_malformed_edit_is_rejected_instead_of_becoming_a_click() -> None:
    result = SessionEditCommandParser().parse(
        "delete something somewhere", mode="commands", language="en"
    )

    assert result.recognized
    assert result.request is None
    assert result.reason_code == "invalid_edit_command"


def test_delete_last_word_removes_its_separator_and_punctuation() -> None:
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_WORD, language="ru"  # type: ignore[arg-type]
    )
    plan = plan_session_edit("Первое второе.", request)

    assert plan.selected_text == " второе."
    assert plan.result_text == "Первое"


def test_delete_last_sentence_keeps_previous_sentence_clean() -> None:
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_SENTENCE, language="ru"  # type: ignore[arg-type]
    )
    plan = plan_session_edit("Первое предложение. Второе предложение!", request)

    assert plan.selected_text == " Второе предложение!"
    assert plan.result_text == "Первое предложение."


def test_replace_last_sentence_preserves_existing_separator() -> None:
    request = SessionEditRequest(
        SessionEditAction.REPLACE_LAST_SENTENCE,
        language="en",  # type: ignore[arg-type]
        new_text="New ending.",
    )
    plan = plan_session_edit("First. Old ending.", request)

    assert plan.replacement_text == " New ending."
    assert plan.result_text == "First. New ending."


def test_unique_replacement_is_case_insensitive_but_whole_token_bounded() -> None:
    request = SessionEditRequest(
        SessionEditAction.REPLACE_UNIQUE,
        language="ru",  # type: ignore[arg-type]
        old_text="андрей",
        new_text="Андрея",
    )
    plan = plan_session_edit("Привет, Андрей!", request)

    assert plan.selected_text == "Андрей"
    assert plan.result_text == "Привет, Андрея!"


@pytest.mark.parametrize(
    ("text", "old", "reason"),
    (
        ("Один два один", "один", "replacement_ambiguous"),
        ("Один два", "три", "replacement_not_found"),
    ),
)
def test_unique_replacement_refuses_ambiguous_or_missing_text(
    text: str, old: str, reason: str
) -> None:
    request = SessionEditRequest(
        SessionEditAction.REPLACE_UNIQUE,
        language="ru",  # type: ignore[arg-type]
        old_text=old,
        new_text="готово",
    )

    with pytest.raises(SessionEditPlanError, match=reason):
        plan_session_edit(text, request)


def test_restore_requires_a_bound_transaction_in_the_app_layer() -> None:
    request = SessionEditRequest(
        SessionEditAction.RESTORE_LAST_EDIT, language="en"  # type: ignore[arg-type]
    )

    with pytest.raises(SessionEditPlanError, match="restore_requires_transaction"):
        plan_session_edit("text", request)
