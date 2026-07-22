from __future__ import annotations

import pickle
import threading

import pytest

from voicetype_local.correction import (
    ContextualReplacementRule,
    CorrectionContext,
    CorrectionPipeline,
    LocalContextualCorrector,
    RamContextBuffer,
    gentle_correct_text,
    protected_tokens,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _github_rule(*, language: str, cue: str, alias: str) -> ContextualReplacementRule:
    return ContextualReplacementRule(
        alias=alias,
        replacement="GitHub",
        context_terms=(cue,),
        language=language,
    )


def test_ram_context_is_bounded_expires_and_can_be_cleared() -> None:
    clock = _Clock()
    memory = RamContextBuffer(
        max_entries=2,
        max_characters=12,
        ttl_seconds=10,
        clock=clock,
    )

    memory.remember("alpha", language="en")
    memory.remember("beta", language="en")
    memory.remember("gamma", language="en")

    assert memory.snapshot(language="en") == ("beta", "gamma")
    assert len(memory) == 2
    assert memory.character_count == 9

    memory.clear()
    assert memory.snapshot() == ()
    memory.remember("recent", language="en")
    clock.value = 10.0
    assert memory.snapshot(language="en") == ()


def test_ram_context_redacts_sensitive_values_and_does_not_disclose_in_repr() -> None:
    memory = RamContextBuffer()
    memory.remember(
        "SecretPhrase a@b.es AB-123 17.07.2026 1 250 € https://example.com foo_bar()",
        language="en",
    )

    stored = " ".join(memory.snapshot(language="en"))
    for sensitive in (
        "a@b.es",
        "AB-123",
        "17.07.2026",
        "1 250 €",
        "https://example.com",
        "foo_bar()",
    ):
        assert sensitive not in stored
    assert "SecretPhrase" in stored
    assert "SecretPhrase" not in repr(memory)
    with pytest.raises(TypeError):
        pickle.dumps(memory)


@pytest.mark.parametrize(
    ("language", "source", "rule", "expected"),
    (
        (
            "ru",
            "репозиторий гит хаб готов",
            _github_rule(language="ru", cue="репозиторий", alias="гит хаб"),
            "Репозиторий GitHub готов",
        ),
        (
            "en",
            "repository git hub ready",
            _github_rule(language="en", cue="repository", alias="git hub"),
            "Repository GitHub ready",
        ),
        (
            "es",
            "repositorio guit hub listo",
            _github_rule(language="es", cue="repositorio", alias="guit hub"),
            "Repositorio GitHub listo",
        ),
    ),
)
def test_exact_contextual_rules_are_safe_for_ru_en_es(
    language: str,
    source: str,
    rule: ContextualReplacementRule,
    expected: str,
) -> None:
    result = LocalContextualCorrector((rule,)).correct(
        source,
        CorrectionContext(language=language),
    )

    assert result.text == expected
    assert result.changed
    assert result.fallback_reason is None


def test_recent_ram_context_can_gate_a_rule_but_wrong_language_cannot() -> None:
    memory = RamContextBuffer()
    memory.remember("Работаем с репозиторием", language="ru")
    provider = LocalContextualCorrector(
        (_github_rule(language="ru", cue="репозиторием", alias="гит хаб"),),
        memory=memory,
    )

    corrected = provider.correct("гит хаб готов", CorrectionContext(language="ru"))
    not_ru = provider.correct("гит хаб готов", CorrectionContext(language="en"))

    assert corrected.text == "GitHub готов"
    assert not_ru.text == "Гит хаб готов"


def test_missing_context_never_guesses_a_replacement() -> None:
    provider = LocalContextualCorrector(
        (_github_rule(language="ru", cue="репозиторий", alias="гит хаб"),)
    )

    result = provider.correct("гит хаб готов", CorrectionContext(language="ru"))

    assert result.text == "Гит хаб готов"
    assert result.fallback_reason is None


def test_conflicting_contextual_rules_fall_back_to_raw_text() -> None:
    rules = (
        ContextualReplacementRule(
            "флоу",
            "Flow",
            ("диктовка",),
            language="ru",
        ),
        ContextualReplacementRule(
            "флоу",
            "Wispr Flow",
            ("помощник",),
            language="ru",
        ),
    )
    source = "диктовка помощник флоу"

    result = LocalContextualCorrector(rules).correct(
        source,
        CorrectionContext(language="ru"),
    )

    assert result.text == source
    assert not result.changed
    assert result.fallback_reason == "ambiguous_contextual_replacement"


def test_contextual_correction_preserves_sensitive_values_byte_for_byte() -> None:
    source = (
        "репозиторий гит хаб ,AB-123 ,17.07.2026 ,1 250 € ,"
        "https://example.com/a,b ,a@b.es ,foo_bar()"
    )
    provider = LocalContextualCorrector(
        (_github_rule(language="ru", cue="репозиторий", alias="гит хаб"),)
    )

    result = provider.correct(source, CorrectionContext(language="ru"))

    assert result.text.startswith("Репозиторий GitHub")
    for protected in (
        "AB-123",
        "17.07.2026",
        "1 250 €",
        "https://example.com/a,b",
        "a@b.es",
        "foo_bar()",
    ):
        assert protected in result.text


def test_rule_that_introduces_a_number_falls_back_to_raw_text() -> None:
    source = "цена пять евро"
    provider = LocalContextualCorrector(
        (
            ContextualReplacementRule(
                "пять",
                "5",
                ("цена",),
                language="ru",
            ),
        )
    )

    result = provider.correct(source, CorrectionContext(language="ru"))

    assert result.text == source
    assert not result.changed
    assert result.fallback_reason == "contextual_protection_failed"


def test_normalizer_error_falls_back_to_raw_text_without_raising() -> None:
    source = "repository git hub"

    def broken_normalizer(text: str) -> str:
        del text
        raise RuntimeError("failure without transcript logging")

    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),),
        normalizer=broken_normalizer,
    )

    result = provider.correct(source, CorrectionContext(language="en"))

    assert result.text == source
    assert not result.changed
    assert result.fallback_reason == "contextual_error:RuntimeError"


def test_correction_pipeline_accepts_only_the_exact_built_in_contextual_provider() -> None:
    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),)
    )

    result = CorrectionPipeline(provider).correct(
        "repository git hub",
        CorrectionContext(language="en"),
    )

    assert result.text == "Repository GitHub"
    assert result.changed


def test_contextual_rule_rejects_unsupported_language_and_self_context() -> None:
    with pytest.raises(ValueError):
        _github_rule(language="fr", cue="dépôt", alias="git hub")
    with pytest.raises(ValueError):
        ContextualReplacementRule(
            "git hub",
            "GitHub",
            ("git hub",),
            language="en",
        )


@pytest.mark.parametrize(
    ("source", "expected", "protected"),
    (
        ("use foo.bar", "Use foo.bar", "foo.bar"),
        ("run object.method()", "Run object.method()", "object.method()"),
        ("edit app.py", "Edit app.py", "app.py"),
        ("check app.py", "Check app.py", "app.py"),
        ("проверь app.py", "Проверь app.py", "app.py"),
        ("read my-file.py", "Read my-file.py", "my-file.py"),
        ("open .env", "Open .env", ".env"),
        ("say foo.bar", "Say foo.bar", "foo.bar"),
        ("открой foo.bar", "Открой foo.bar", "foo.bar"),
        (
            r"open C:\Program Files\VoiceType\app.exe",
            r"Open C:\Program Files\VoiceType\app.exe",
            r"C:\Program Files\VoiceType\app.exe",
        ),
        (
            "open D:/Program Files/VoiceType/app.exe",
            "Open D:/Program Files/VoiceType/app.exe",
            "D:/Program Files/VoiceType/app.exe",
        ),
        (
            "open /home/user/my file.txt",
            "Open /home/user/my file.txt",
            "/home/user/my file.txt",
        ),
        (
            "visit example.com?x=1",
            "Visit example.com?x=1",
            "example.com?x=1",
        ),
        (
            "visit example.com:8080/path",
            "Visit example.com:8080/path",
            "example.com:8080/path",
        ),
        (
            "connect 127.0.0.1:8080",
            "Connect 127.0.0.1:8080",
            "127.0.0.1:8080",
        ),
        ("version v1.2.3 ready", "Version v1.2.3 ready", "v1.2.3"),
        ("Python 3.12.0rc1 ready", "Python 3.12.0rc1 ready", "3.12.0rc1"),
        (
            "version 1.2.3-rc.1+build.5 ready",
            "Version 1.2.3-rc.1+build.5 ready",
            "1.2.3-rc.1+build.5",
        ),
        ("version v1.2.3_beta", "Version v1.2.3_beta", "v1.2.3_beta"),
        ("use ftp://server/path", "Use ftp://server/path", "ftp://server/path"),
        ("use ws://localhost:3000/x", "Use ws://localhost:3000/x", "ws://localhost:3000/x"),
        ("use wss://example.com/x", "Use wss://example.com/x", "wss://example.com/x"),
        ("open file:///C:/Temp/a.txt", "Open file:///C:/Temp/a.txt", "file:///C:/Temp/a.txt"),
        ("open s3://bucket/key", "Open s3://bucket/key", "s3://bucket/key"),
        (
            "use postgresql://user:pass@server:5432/db",
            "Use postgresql://user:pass@server:5432/db",
            "postgresql://user:pass@server:5432/db",
        ),
        ("send mailto:user@example.com", "Send mailto:user@example.com", "mailto:user@example.com"),
        ("use urn:isbn:9780140328721", "Use urn:isbn:9780140328721", "urn:isbn:9780140328721"),
        (
            "clone git@github.com:user/repo.git",
            "Clone git@github.com:user/repo.git",
            "git@github.com:user/repo.git",
        ),
        ("connect [::1]:8080", "Connect [::1]:8080", "[::1]:8080"),
        ("connect server:8080", "Connect server:8080", "server:8080"),
        ("open api:3000/path", "Open api:3000/path", "api:3000/path"),
        ("open src/app.py", "Open src/app.py", "src/app.py"),
        (r"open src\app.py", r"Open src\app.py", r"src\app.py"),
        ("open ./src/app.py", "Open ./src/app.py", "./src/app.py"),
        ("open ../src/app.py", "Open ../src/app.py", "../src/app.py"),
        ("checkout feature/foo", "Checkout feature/foo", "feature/foo"),
        ("open .bashrc", "Open .bashrc", ".bashrc"),
        ("open .editorconfig", "Open .editorconfig", ".editorconfig"),
        ("open .env.local now", "Open .env.local now", ".env.local"),
        (
            "open .gitlab-ci.yml now",
            "Open .gitlab-ci.yml now",
            ".gitlab-ci.yml",
        ),
        (
            "open .vscode/settings.json",
            "Open .vscode/settings.json",
            ".vscode/settings.json",
        ),
        ("use .NET", "Use .NET", ".NET"),
        (
            "email пользователь@пример.рф",
            "Email пользователь@пример.рф",
            "пользователь@пример.рф",
        ),
        ("email user@mañana.com", "Email user@mañana.com", "user@mañana.com"),
        ("visit пример.рф/путь", "Visit пример.рф/путь", "пример.рф/путь"),
        ("visit пример.рф now", "Visit пример.рф now", "пример.рф"),
        ("сайт mañana.com работает", "Сайт mañana.com работает", "mañana.com"),
        ("visit 例子.测试 now", "Visit 例子.测试 now", "例子.测试"),
        (
            "open xn--fsqu00a.xn--0zwm56d now",
            "Open xn--fsqu00a.xn--0zwm56d now",
            "xn--fsqu00a.xn--0zwm56d",
        ),
        ("visit example.shop now", "Visit example.shop now", "example.shop"),
        (
            "site example.museum works",
            "Site example.museum works",
            "example.museum",
        ),
        (
            "visit пример.рф/путь?ключ=знач now",
            "Visit пример.рф/путь?ключ=знач now",
            "пример.рф/путь?ключ=знач",
        ),
        ("x=1", "x=1", "x=1"),
        ("foo=bar", "foo=bar", "foo=bar"),
        ("set x=1. next", "Set x=1. Next", "x=1"),
        ("set foo=bar? next", "Set foo=bar? Next", "foo=bar"),
        ("x=1,foo=2", "x=1,foo=2", "x=1,foo=2"),
        ("x=1;y=2", "x=1;y=2", "x=1;y=2"),
        ("x=[1,2]", "x=[1,2]", "x=[1,2]"),
        ("x=(1,2)", "x=(1,2)", "x=(1,2)"),
        ('x="hello,world"', 'x="hello,world"', 'x="hello,world"'),
        (
            'config={"a":1,"b":2}',
            'config={"a":1,"b":2}',
            'config={"a":1,"b":2}',
        ),
        (
            '{"safe":{"x":1},"password":"secret,value"}',
            '{"safe":{"x":1},"password":"secret,value"}',
            '{"safe":{"x":1},"password":"secret,value"}',
        ),
        (
            'config={"safe":{"x":1},"password":"secret,value"}',
            'config={"safe":{"x":1},"password":"secret,value"}',
            'config={"safe":{"x":1},"password":"secret,value"}',
        ),
        (
            '{\n"password":"secret,value",\n"nested":{"x":1}\n}',
            '{\n"password":"secret,value",\n"nested":{"x":1}\n}',
            '{\n"password":"secret,value",\n"nested":{"x":1}\n}',
        ),
        (
            'config={\n"password":"secret,value",\n"x":1\n}',
            'config={\n"password":"secret,value",\n"x":1\n}',
            'config={\n"password":"secret,value",\n"x":1\n}',
        ),
        (
            '{"password":"secret,value"',
            '{"password":"secret,value"',
            '{"password":"secret,value"',
        ),
        ("x=[1,[2,3],4]", "x=[1,[2,3],4]", "x=[1,[2,3],4]"),
        (
            "password=`secret,value`",
            "password=`secret,value`",
            "password=`secret,value`",
        ),
        (
            '$env:API_KEY="secret,value"',
            '$env:API_KEY="secret,value"',
            '$env:API_KEY="secret,value"',
        ),
        (
            "$env:KEY='secret,''value'",
            "$env:KEY='secret,''value'",
            "$env:KEY='secret,''value'",
        ),
        ('$token=`secret,value`', '$token=`secret,value`', '$token=`secret,value`'),
        (
            '$global:token="secret,value"',
            '$global:token="secret,value"',
            '$global:token="secret,value"',
        ),
        (
            '$items[0]="secret,value"',
            '$items[0]="secret,value"',
            '$items[0]="secret,value"',
        ),
        ('pattern=r"a,b"', 'pattern=r"a,b"', 'pattern=r"a,b"'),
        (
            'message=f"hello,{name}"',
            'message=f"hello,{name}"',
            'message=f"hello,{name}"',
        ),
        (
            'text="""hello,world"""',
            'text="""hello,world"""',
            'text="""hello,world"""',
        ),
        ('data-key="a,b"', 'data-key="a,b"', 'data-key="a,b"'),
        ('arr[0]="a,b"', 'arr[0]="a,b"', 'arr[0]="a,b"'),
        ('password=secret,value', 'password=secret,value', 'password=secret,value'),
        ('token=abc;def', 'token=abc;def', 'token=abc;def'),
        ("open src/app.py. next", "Open src/app.py. Next", "src/app.py"),
        ("src/foo,bar.py", "src/foo,bar.py", "src/foo,bar.py"),
        ("src/foo;bar.py", "src/foo;bar.py", "src/foo;bar.py"),
        ("[fe80::1%25eth0]:8080", "[fe80::1%25eth0]:8080", "[fe80::1%25eth0]:8080"),
        (
            "git@[2001:db8::1]:repo.git",
            "git@[2001:db8::1]:repo.git",
            "git@[2001:db8::1]:repo.git",
        ),
        ("3proxy:8080", "3proxy:8080", "3proxy:8080"),
        (
            "AA:BB:CC:DD:EE:FF",
            "AA:BB:CC:DD:EE:FF",
            "AA:BB:CC:DD:EE:FF",
        ),
        ("2001:db8::1", "2001:db8::1", "2001:db8::1"),
        ("nginx:1.25-alpine", "nginx:1.25-alpine", "nginx:1.25-alpine"),
        ("score 3:2 now", "Score 3:2 now", "3:2"),
        ("use ssh:user@example.com", "Use ssh:user@example.com", "ssh:user@example.com"),
        ("use sip:user@example.com", "Use sip:user@example.com", "sip:user@example.com"),
        ("use geo:40.7,-74.0", "Use geo:40.7,-74.0", "geo:40.7,-74.0"),
        ("{key:value}", "{key:value}", "{key:value}"),
        ("[foo,bar]", "[foo,bar]", "[foo,bar]"),
    ),
)
def test_code_like_values_are_atomically_protected(
    source: str,
    expected: str,
    protected: str,
) -> None:
    corrected = gentle_correct_text(source)

    assert corrected == expected
    assert protected in protected_tokens(source)
    assert protected in protected_tokens(corrected)


def test_balanced_url_parenthesis_is_part_of_url_but_wrapper_is_not() -> None:
    balanced = "open https://example.com/wiki/Foo_(bar) now"
    wrapped = "open (https://example.com/wiki/Foo_(bar)). next"

    assert gentle_correct_text(balanced) == (
        "Open https://example.com/wiki/Foo_(bar) now"
    )
    assert "https://example.com/wiki/Foo_(bar)" in protected_tokens(balanced)
    assert gentle_correct_text(wrapped) == (
        "Open (https://example.com/wiki/Foo_(bar)). Next"
    )


def test_natural_two_sentence_text_is_not_mistaken_for_dotted_code() -> None:
    assert gentle_correct_text("привет.мир") == "Привет. Мир"
    assert gentle_correct_text("hello.world") == "Hello. World"
    assert gentle_correct_text("hola ,mundo.esto funciona") == (
        "Hola, mundo. Esto funciona"
    )
    assert gentle_correct_text("mañana ,todo.bien") == "Mañana, todo. Bien"
    assert gentle_correct_text("hello.world.this works") == (
        "Hello. World. This works"
    )
    assert gentle_correct_text("hola.mundo.esto funciona") == (
        "Hola. Mundo. Esto funciona"
    )
    assert gentle_correct_text("привет.мир.это работает") == (
        "Привет. Мир. Это работает"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("note:hello world", "Note: hello world"),
        ("chapter:123 next", "Chapter: 123 next"),
        ("это пункт:123 следующий", "Это пункт: 123 следующий"),
        ("visita mañana.es ahora", "Visita mañana. Es ahora"),
    ),
)
def test_normal_colons_and_sentence_dots_are_not_mistaken_for_code(
    source: str,
    expected: str,
) -> None:
    assert gentle_correct_text(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("version:1.25 is ready", "Version: 1.25 is ready"),
        ("chapter:1.25 is ready", "Chapter: 1.25 is ready"),
        ("image nginx:1.25", "Image nginx:1.25"),
    ),
)
def test_docker_tag_protection_requires_technical_evidence(
    source: str,
    expected: str,
) -> None:
    assert gentle_correct_text(source) == expected


def test_dot_after_a_closing_quote_is_sentence_punctuation_not_a_dotfile() -> None:
    assert gentle_correct_text('"hello".next sentence') == (
        '"Hello". Next sentence'
    )
    assert gentle_correct_text("«привет».дальше") == "«Привет». Дальше"


def test_unicode_ellipsis_starts_a_new_sentence() -> None:
    assert gentle_correct_text("привет…как дела") == "Привет… Как дела"


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("т.е.это", "т.е. Это"),
        ("e.g.this", "e.g. This"),
        ("U.S.A.is", "U.S.A. Is"),
    ),
)
def test_abbreviation_boundary_is_not_left_glued(
    source: str,
    expected: str,
) -> None:
    assert gentle_correct_text(source) == expected


def test_ram_context_redacts_bare_idn_domains() -> None:
    memory = RamContextBuffer()

    memory.remember("visit пример.рф now", language="en")

    stored = " ".join(memory.snapshot(language="en"))
    assert "пример.рф" not in stored
    assert stored == "visit now"


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("да/нет это ответ", "Да/нет это ответ"),
        ("yes/no is valid", "Yes/no is valid"),
        ("sí/no es válido", "Sí/no es válido"),
    ),
)
def test_natural_slash_phrases_are_not_mistaken_for_paths(
    source: str,
    expected: str,
) -> None:
    assert gentle_correct_text(source) == expected


@pytest.mark.parametrize(
    "source",
    (
        'password="secret,value"',
        'config={"api_key":"secret,value","x":2}',
        'password="secret,\\"value"',
        'password=`secret,value`',
        '{"safe":{"x":1},"password":"secret,value"}',
        'config={"safe":{"x":1},"password":"secret,value"}',
        '$env:API_KEY="secret,value"',
        '{\n"password":"secret,value",\n"nested":{"x":1}\n}',
        'config={\n"password":"secret,value",\n"x":1\n}',
        '{"password":"secret,value"',
        'password=secret,value',
        'token=abc;def',
        '$token=`secret,value`',
        '$global:token="secret,value"',
        '$items[0]="secret,value"',
        "$env:KEY='secret,''value'",
        'pattern=r"a,b"',
        'message=f"hello,{name}"',
        'text="""hello,world"""',
        'data-key="a,b"',
        'arr[0]="a,b"',
    ),
)
def test_ram_context_redacts_complete_assignment_values(source: str) -> None:
    memory = RamContextBuffer()

    memory.remember(source, language="en")

    assert memory.snapshot(language="en") == ()


def test_normalized_context_terms_are_deduplicated_before_minimum_validation() -> None:
    rule = ContextualReplacementRule(
        "git hub",
        "GitHub",
        ("repository", "Repository"),
        language="en",
        minimum_context_matches=2,
    )
    provider = LocalContextualCorrector((rule,))

    result = provider.correct(
        "repository git hub",
        CorrectionContext(language="en"),
    )

    assert rule.context_terms == ("repository",)
    assert result.text == "Repository git hub"


def test_context_term_cannot_be_a_substring_of_alias() -> None:
    with pytest.raises(ValueError):
        ContextualReplacementRule(
            "git hub",
            "GitHub",
            ("git",),
            language="en",
        )


def test_context_gate_applies_to_each_occurrence_in_a_limited_window() -> None:
    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),)
    )
    separator = (
        " alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo"
        " lima mike november oscar papa quebec romeo sierra tango uniform"
        " victor whiskey xray yankee zulu"
    )
    source = f"repository git hub{separator} git hub"

    result = provider.correct(source, CorrectionContext(language="en"))

    assert result.text == f"Repository GitHub{separator} git hub"
    assert result.text.count("GitHub") == 1


def test_context_window_never_creates_a_cue_from_the_tail_of_a_word() -> None:
    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),)
    )
    # The alias begins at offset 99, so a raw 96-character slice would begin
    # exactly at the embedded "repository" inside "notrepository".
    source = "notrepository" + (" " * 86) + "git hub"

    result = provider.correct(source, CorrectionContext(language="en"))

    assert "GitHub" not in result.text
    assert result.text.endswith("git hub")


def test_auto_language_does_not_read_or_write_language_scoped_ram_context() -> None:
    memory = RamContextBuffer()
    memory.remember("repository", language="en")
    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),),
        memory=memory,
    )

    result = provider.correct("git hub ready", CorrectionContext(language="auto"))

    assert result.text == "Git hub ready"
    assert memory.snapshot(language="en") == ("repository",)
    assert memory.snapshot(language="auto") == ()


def test_ram_context_never_blanket_changes_multiple_distant_occurrences() -> None:
    memory = RamContextBuffer()
    memory.remember("repository", language="en")
    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),),
        memory=memory,
    )
    separator = " alpha bravo charlie delta echo foxtrot golf hotel" * 4

    result = provider.correct(
        f"git hub{separator} git hub",
        CorrectionContext(language="en"),
    )

    assert "GitHub" not in result.text
    assert result.text.endswith("git hub")


def test_ram_truncation_drops_a_partial_leading_word_instead_of_making_a_cue() -> None:
    memory = RamContextBuffer(max_characters=10)

    memory.remember("notrepository", language="en")

    assert memory.snapshot(language="en") == ()


def test_clear_context_prevents_in_flight_correction_from_repopulating_memory() -> None:
    memory = RamContextBuffer()
    memory.remember("repository", language="en")
    normalizer_started = threading.Event()
    release_normalizer = threading.Event()

    def blocking_normalizer(text: str) -> str:
        normalizer_started.set()
        assert release_normalizer.wait(timeout=2)
        return text

    provider = LocalContextualCorrector(
        (_github_rule(language="en", cue="repository", alias="git hub"),),
        memory=memory,
        normalizer=blocking_normalizer,
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            provider.correct("git hub ready", CorrectionContext(language="en"))
        )
    )
    worker.start()
    assert normalizer_started.wait(timeout=2)

    provider.clear_context()
    release_normalizer.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert results and results[0].text == "GitHub ready"
    assert memory.snapshot(language="en") == ()


def test_ttl_cleanup_is_explicit_and_does_not_create_background_threads() -> None:
    clock = _Clock()
    before = {thread.ident for thread in threading.enumerate()}
    memory = RamContextBuffer(ttl_seconds=5, clock=clock)
    memory.remember("temporary context", language="en")
    clock.value = 5

    assert memory.prune() == 1
    assert memory.snapshot(language="en") == ()
    assert {thread.ident for thread in threading.enumerate()} == before
