# VoiceType Local

[![Tests](https://github.com/andreinv-crypto/voicetype-local/actions/workflows/tests.yml/badge.svg)](https://github.com/andreinv-crypto/voicetype-local/actions/workflows/tests.yml)

**Public preview:** [download VoiceType Local 0.2.0-rc1](https://github.com/andreinv-crypto/voicetype-local/releases/tag/v0.2.0-rc1)

VoiceType Local — accessibility-first голосовой ввод для Windows 10/11.

1. Поставьте курсор в любое текстовое поле.
2. Один раз нажмите правый Ctrl — начнётся запись.
3. Говорите.
4. Нажмите правый Ctrl ещё раз — запись остановится, а текст появится в том же
   поле.

Правый Ctrl — единственная обязательная клавиша по умолчанию. Удержание и
автоповтор дают только одно действие. Другие клавиши и мышь не включают и не
выключают запись. В настройках можно выбрать F8, F9, F10 или Pause для внешнего
accessibility-переключателя.

## Что реализовано в preview 0.2.0-rc1

- полностью локальный Whisper `small`, CPU `int8`;
- русский, испанский, английский и смешанная речь;
- отдельный перезапускаемый Whisper-процесс с timeout и одним retry;
- WASAPI-first микрофон и восстановление после зависшего start/stop;
- проверка конкретного focused field через Win32 и UI Automation;
- безопасное состояние «Текст сохранён», если поле изменилось или не определено;
- Unicode-вставка без изменения clipboard;
- неактивирующий overlay и запасной tray;
- SQLite-память терминов, алиасов, областей и стилей;
- ограниченный `initial_prompt`/`hotwords`, полный словарь в Whisper не попадает;
- однозначные замены только по целым словам с защитой чисел, дат, сумм, URL,
  email и кодов;
- опциональная облачная text-only коррекция и локальный Ollama-adapter;
- отдельный добровольный режим облачного распознавания аудио;
- API-ключи только через Windows DPAPI;
- вращающийся технический журнал без аудио, текста и словаря;
- доступное окно настроек с раздельными согласиями для текста и аудио.

Все облачные и дополнительные модельные режимы выключены по умолчанию. Обычная
диктовка не требует аккаунта, API-ключа, Ollama или интернета.

## Память без «засорения»

VoiceType не сохраняет каждую диктовку и не следит за исправлениями в других
программах. Постоянная пользовательская запись появляется только после явного
добавления или подтверждённого импорта. В комплекте также есть выключенные
нейтральные тематические наборы: они ничего не меняют, пока пользователь сам их
не включит.

Локальные данные:

```text
%LOCALAPPDATA%\VoiceTypeLocal\settings.json
%LOCALAPPDATA%\VoiceTypeLocal\data\memory.sqlite3
%LOCALAPPDATA%\VoiceTypeLocal\secrets\*.dpapi
%LOCALAPPDATA%\VoiceTypeLocal\logs\voicetype.log*
```

Аудио и расшифровка текущей диктовки остаются только в RAM. Подробнее:
[PRIVACY.md](PRIVACY.md) и
[архитектура приложения](docs/ARCHITECTURE.md).

## Разработка и тесты

```powershell
.\setup.ps1
.\.venv\Scripts\python.exe -m pytest -q
.\build.ps1
```

`build.ps1` не перезаписывает запущенную стабильную версию. Кандидат создаётся в:

```text
dist_candidate\VoiceType Local\VoiceType Local.exe
```

Скрипт выполняет unit-тесты, packaged self-test и UI smoke-test. Установка в
`%LOCALAPPDATA%\Programs\VoiceType Local` и изменение ярлыков выполняются только
после отдельного подтверждения через `install_candidate.ps1`. Есть автоматический
rollback; подробнее в [docs/ROLLBACK.md](docs/ROLLBACK.md).

## Готовая Windows-сборка

На странице [предварительного релиза](https://github.com/andreinv-crypto/voicetype-local/releases/tag/v0.2.0-rc1)
скачайте ZIP из раздела **Assets**, распакуйте его целиком и запустите
`VoiceType Local\VoiceType Local.exe`. Сборка пока не подписана сертификатом;
Windows SmartScreen может показать предупреждение.

## Evaluation

В `eval/cases.json` есть 24 нейтральные контрольные фразы на русском, испанском,
английском и смешанном тексте. Проверка манифеста не читает аудио:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_transcripts.py
```

Реальные голосовые образцы добавляются только добровольно и исключены из Git.

## Текущий статус

Версия 0.2.0-rc1 публикуется как предварительная source-available сборка для
личного некоммерческого тестирования. Это не финальный подписанный релиз.
Коммерческое использование, перепродажа, перепаковка и распространение требуют
письменного разрешения. Ссылкой на официальный репозиторий или релиз делиться
можно; подробнее в [LICENSE](LICENSE). На этапе preview внешние фрагменты кода и
pull request принимаются только после отдельного письменного соглашения — см.
[CONTRIBUTING.md](CONTRIBUTING.md).
