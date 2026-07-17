from __future__ import annotations

from dataclasses import fields

import pytest

from voicetype_local.dictation_transform import (
    AppliedDictationCommand,
    DictationCommand,
    DictationLanguage,
    apply_short_phrase_period_policy,
    combine_insertions,
    transform_dictation,
)


def test_russian_embedded_layout_and_punctuation_commands() -> None:
    result = transform_dictation(
        "Привет запятая мир точка новая строка Как дела вопросительный знак "
        "новый абзац Отлично восклицательный знак",
        language="ru",
    )

    assert result.text == "Привет, мир.\nКак дела?\n\nОтлично!"
    assert [item.command for item in result.applied_commands] == [
        DictationCommand.COMMA,
        DictationCommand.PERIOD,
        DictationCommand.NEW_LINE,
        DictationCommand.QUESTION_MARK,
        DictationCommand.NEW_PARAGRAPH,
        DictationCommand.EXCLAMATION_MARK,
    ]
    assert all(item.language is DictationLanguage.RU for item in result.applied_commands)
    assert result.changed


def test_english_tab_paragraph_colon_and_semicolon() -> None:
    result = transform_dictation(
        "Heading colon new paragraph first tab second semicolon third",
        language=DictationLanguage.EN,
    )

    assert result.text == "Heading:\n\nfirst\tsecond; third"
    assert [item.command for item in result.applied_commands] == [
        DictationCommand.COLON,
        DictationCommand.NEW_PARAGRAPH,
        DictationCommand.TAB,
        DictationCommand.SEMICOLON,
    ]


def test_spanish_accented_phrases_are_supported() -> None:
    result = transform_dictation(
        "Hola coma mundo nueva línea siguiente nuevo párrafo pregunta "
        "signo de interrogación",
        language="es",
    )

    assert result.text == "Hola, mundo\nsiguiente\n\npregunta?"
    assert all(item.language is DictationLanguage.ES for item in result.applied_commands)


@pytest.mark.parametrize(
    ("text", "expected", "command"),
    [
        ("один двоеточие два", "один: два", DictationCommand.COLON),
        ("one full stop two", "one. two", DictationCommand.PERIOD),
        ("uno punto y coma dos", "uno; dos", DictationCommand.SEMICOLON),
        ("uno dos puntos dos", "uno: dos", DictationCommand.COLON),
    ],
)
def test_exact_multilingual_phrases_in_auto_mode(
    text: str, expected: str, command: DictationCommand
) -> None:
    result = transform_dictation(text, language="auto")

    assert result.text == expected
    assert result.applied_commands[0].command is command


def test_auto_mode_can_apply_different_languages_in_one_utterance() -> None:
    result = transform_dictation("Привет запятая hello new line hola punto", language="auto")

    assert result.text == "Привет, hello\nhola."
    assert [item.language for item in result.applied_commands] == [
        DictationLanguage.RU,
        DictationLanguage.EN,
        DictationLanguage.ES,
    ]


def test_explicit_language_does_not_accept_other_language_commands() -> None:
    result = transform_dictation("hello comma world", language="ru")

    assert result.text == "hello comma world"
    assert result.applied_commands == ()
    assert not result.changed


@pytest.mark.parametrize(
    "text",
    [
        "deadline tabloid periodic semicolonish",
        "новая строками и запятаяданные",
        "nuevamente lineal puntocom",
        "new-line new_line",
    ],
)
def test_commands_require_exact_word_boundaries(text: str) -> None:
    result = transform_dictation(text, language="auto")

    assert result.text == text
    assert result.applied_commands == ()


def test_longest_spanish_punctuation_phrase_wins() -> None:
    result = transform_dictation("uno punto y coma dos", language="es")

    assert result.text == "uno; dos"
    assert [item.command for item in result.applied_commands] == [
        DictationCommand.SEMICOLON
    ]


def test_urls_emails_codes_and_numbers_are_not_searched_for_commands() -> None:
    source = "new_line@example.com NEW-LINE AB-123 X1234567L 12:30 1 250 €"
    result = transform_dictation(source, language="auto", remove_fillers=True)

    assert result.text == source
    assert result.applied_commands == ()
    assert result.fillers_removed == 0


def test_uppercase_code_words_are_preserved_instead_of_treated_as_commands() -> None:
    source = "NEW LINE PERIOD"
    result = transform_dictation(source, language="en")

    assert result.text == source
    assert result.applied_commands == ()


def test_command_next_to_protected_values_changes_only_command_phrase() -> None:
    source = "https://example.com точка Цена 1 250 € запятая код X1234567L"
    result = transform_dictation(source, language="ru")

    assert result.text == "https://example.com. Цена 1 250 €, код X1234567L"
    for protected in ("https://example.com", "1 250 €", "X1234567L"):
        assert protected in result.text


def test_direct_template_match_is_complete_casefolded_and_space_normalized() -> None:
    content = "С уважением,\nАндрей\nhttps://example.com\nКод AB-123"
    result = transform_dictation(
        "   ПОДПИСЬ   ",
        language="ru",
        templates={"подпись": content},
    )

    assert result.text == content
    assert result.applied_template == "подпись"
    assert result.applied_commands == ()
    assert result.fillers_removed == 0


@pytest.mark.parametrize(
    ("text", "language", "name"),
    [
        ("шаблон подпись", "ru", "подпись"),
        ("template signature", "en", "signature"),
        ("plantilla firma rápida", "es", "firma rápida"),
        ("PLANTILLA   FIRMA RAPIDA", "auto", "firma rápida"),
    ],
)
def test_template_prefix_must_cover_the_complete_utterance(
    text: str, language: str, name: str
) -> None:
    result = transform_dictation(text, language=language, templates={name: "CONTENT"})

    assert result.text == "CONTENT"
    assert result.applied_template == name


@pytest.mark.parametrize(
    "text",
    [
        "please template signature",
        "templatex signature",
        "template signature extra",
        "шаблонный подпись",
        "plantillas firma",
    ],
)
def test_template_does_not_expand_on_partial_or_unanchored_request(text: str) -> None:
    result = transform_dictation(
        text,
        language="auto",
        templates={"signature": "CONTENT", "подпись": "CONTENT", "firma": "CONTENT"},
    )

    assert result.applied_template is None
    assert result.text == text


def test_normalized_template_name_collision_fails_closed() -> None:
    result = transform_dictation(
        "resume",
        templates={"Résumé": "FIRST", "resume": "SECOND"},
    )

    assert result.text == "resume"
    assert result.applied_template is None


def test_template_content_is_literal_and_never_recursively_transformed() -> None:
    content = "new line comma um https://example.com"
    result = transform_dictation(
        "template literal",
        language="en",
        templates={"literal": content},
        remove_fillers=True,
    )

    assert result.text == content
    assert result.applied_commands == ()
    assert result.fillers_removed == 0


def test_template_exact_match_takes_priority_over_filler_removal() -> None:
    result = transform_dictation("um", templates={"um": "USER TEMPLATE"}, remove_fillers=True)

    assert result.text == "USER TEMPLATE"
    assert result.applied_template == "um"


def test_malformed_template_mapping_is_rejected() -> None:
    with pytest.raises(TypeError):
        transform_dictation("template one", templates={"one": 1})  # type: ignore[dict-item]


def test_fillers_are_disabled_by_default() -> None:
    source = "I um think, uh, yes"
    result = transform_dictation(source, language="en")

    assert result.text == source
    assert result.fillers_removed == 0


def test_filler_removal_is_independent_from_embedded_commands() -> None:
    result = transform_dictation(
        "um hello comma world",
        language="en",
        commands_enabled=False,
        remove_fillers=True,
    )

    assert result.text == "hello comma world"
    assert result.applied_commands == ()
    assert result.fillers_removed == 1


@pytest.mark.parametrize(
    ("text", "language", "expected", "count"),
    [
        ("эм Привет, ээ, мир", "ru", "Привет, мир", 2),
        ("I um think, uh, yes", "en", "I think, yes", 2),
        ("Hola, eh, mundo", "es", "Hola, mundo", 1),
        ("um hello eh mundo эм привет", "auto", "hello mundo привет", 3),
    ],
)
def test_optional_closed_filler_lists_are_conservative(
    text: str, language: str, expected: str, count: int
) -> None:
    result = transform_dictation(text, language=language, remove_fillers=True)

    assert result.text == expected
    assert result.fillers_removed == count


def test_meaningful_words_are_not_in_filler_list() -> None:
    source = "ну like pues este"
    result = transform_dictation(source, language="auto", remove_fillers=True)

    assert result.text == source
    assert result.fillers_removed == 0


def test_filler_substrings_parentheses_and_protected_tokens_are_preserved() -> None:
    source = "album (um) um@example.com UM-123"
    result = transform_dictation(source, language="en", remove_fillers=True)

    assert result.text == source
    assert result.fillers_removed == 0


def test_result_metadata_has_commands_but_no_source_phrase_field() -> None:
    result = transform_dictation("hello comma world", language="en")

    assert result.text == "hello, world"
    assert [field.name for field in fields(AppliedDictationCommand)] == [
        "command",
        "language",
    ]
    assert not hasattr(result, "source_text")
    assert not hasattr(result, "original_text")


def test_spoken_period_is_preserved_without_auto_period_provenance() -> None:
    transformed = transform_dictation("hello period", language="en").text

    assert transformed == "hello."
    assert (
        apply_short_phrase_period_policy(
            transformed,
            no_final_period_short=True,
            period_was_auto_added=False,
        )
        == "hello."
    )


def test_short_final_period_rule_requires_both_setting_and_provenance() -> None:
    source = "Короткая фраза."

    assert apply_short_phrase_period_policy(source) == source
    assert (
        apply_short_phrase_period_policy(
            source,
            no_final_period_short=True,
            period_was_auto_added=False,
        )
        == source
    )
    assert (
        apply_short_phrase_period_policy(
            source,
            no_final_period_short=True,
            period_was_auto_added=True,
        )
        == "Короткая фраза"
    )


@pytest.mark.parametrize(
    "text",
    [
        "One two three four five six seven eight nine.",
        "Wait...",
        "Question?",
        "First line\nSecond.",
        ".",
    ],
)
def test_short_final_period_rule_fails_closed_for_ambiguous_text(text: str) -> None:
    assert (
        apply_short_phrase_period_policy(
            text,
            no_final_period_short=True,
            period_was_auto_added=True,
        )
        == text
    )


def test_short_final_period_rule_preserves_trailing_horizontal_space() -> None:
    assert (
        apply_short_phrase_period_policy(
            "Short phrase.  ",
            no_final_period_short=True,
            period_was_auto_added=True,
        )
        == "Short phrase  "
    )


@pytest.mark.parametrize("max_words", [0, 51, True, 2.5])
def test_short_final_period_rule_validates_limit(max_words: object) -> None:
    with pytest.raises(ValueError):
        apply_short_phrase_period_policy("Hello.", max_words=max_words)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("Hello", "world", "Hello world"),
        ("Hello.", "Next", "Hello. Next"),
        ("Hello,", "world", "Hello, world"),
        ("Hello ", "world", "Hello world"),
        ("Hello", " world", "Hello world"),
        ("Hello", ", world", "Hello, world"),
        ("(", "word", "(word"),
        ("word", ")", "word)"),
        ("line\n", "next", "line\nnext"),
        ("line", "\nnext", "line\nnext"),
        ("123", "456", "123 456"),
        ("", "text", "text"),
        ("text", "", "text"),
    ],
)
def test_combine_insertions_adds_only_one_safe_separator(
    previous: str, current: str, expected: str
) -> None:
    assert combine_insertions(previous, current) == expected


def test_public_functions_reject_non_string_text() -> None:
    with pytest.raises(TypeError):
        transform_dictation(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        apply_short_phrase_period_policy(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        combine_insertions(None, "text")  # type: ignore[arg-type]


def test_unknown_language_is_not_silently_guessed() -> None:
    with pytest.raises(ValueError):
        transform_dictation("hello comma world", language="unknown")
