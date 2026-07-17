# VoiceType Control — статический security/privacy/a11y audit

Дата: 2026-07-17
Статус: **не готово к публичному выпуску до закрытия release blockers ниже**
Тип проверки: статический обзор исходного кода и проектной документации

> Важно: в рамках этого аудита код не изменялся. Тесты, сборка, запуск приложения,
> динамическая проверка Windows/UI Automation/UAC, скан зависимостей, Git-истории и
> готового дистрибутива **не выполнялись**. «Подтверждено» ниже означает
> «подтверждено чтением текущего исходного кода», а не доказано runtime-тестом.

> **Статус после аудита.** Этот документ сохранён как baseline до remediation и
> поэтому формулирует B1–B8 в исходном виде. После него были исправлены risk
> propagation, sensitive UIA roles/names, Space, target-bound one-shot
> confirmation, executor TTL, foreground/elevation gates, cancellation epoch,
> snapshot cleanup и transactional settings/secret update. Актуальные тестовые
> результаты и оставшиеся dynamic/release gates находятся в
> [`IMPLEMENTATION_STATUS_2026-07-17.md`](IMPLEMENTATION_STATUS_2026-07-17.md).
> Этот baseline нельзя использовать как утверждение, что перечисленные там
> исходные дефекты всё ещё присутствуют в текущей ветке.

## 1. Итог

У VoiceType Control уже есть хорошая защитная основа:

- речь не передаётся в `shell`, `eval`, командную строку или произвольный URL API;
- Windows-команды преобразуются в небольшой типизированный набор действий;
- приложения открываются только из закрытого реестра через `shell=False`;
- Win32-управление блокирует неизвестные, запрещённые и elevated-окна;
- UI Automation не запрашивает `ValuePattern`, текст поля или пароль;
- снимки UIA содержат только ограниченные дескрипторы, а не живые COM-объекты;
- API-ключ не хранится в `settings.json` и защищён Windows DPAPI;
- технические логи используют белый список полей и не принимают диктовку;
- аудио и полный текст диктовки не записываются в историю приложения.

Однако текущая модель безопасности пока разрывается на границе UIA,
подтверждений, потоков и настроек. Самые опасные сценарии:

1. Кнопка `Confirm`, `Authorize`, `Publish`, ссылка, пункт меню или сфокусированная
   кнопка через `Space` могут быть активированы без требуемого подтверждения.
2. Подтверждение можно выдать для действия в окне A, затем выполнить одноимённое
   действие в окне B; клавиши также не привязаны к исходному foreground/focus.
3. Нажатие Escape отменяет результат на уровне интерфейса, но не останавливает
   уже запущенный backend: действие способно произойти позже.
4. Если VoiceType запущен elevated, UIA, вставка текста и запуск разрешённых
   приложений не имеют общего fail-closed барьера.
5. Неудачная запись настроек может оставить новые значения в памяти, а старый
   облачный provider — работающим. Это особенно опасно при попытке отключить
   отправку текста или аудио.
6. Accessible `Name` может сам содержать текст документа, а снимки с такими
   именами после скрытия оверлея остаются в executor/UIA backend до следующего
   снимка или выхода.

## 2. Область аудита

Основные просмотренные границы:

- `voicetype_local/voice_commands.py` — грамматика и запреты;
- `voicetype_local/voice_control.py` — преобразование результата распознавания;
- `voicetype_local/windows_control.py` — typed intents, Win32, UIA, подтверждение;
- `voicetype_local/app.py` — TTL, worker threads, cancel, state/generation;
- `voicetype_local/hotkey.py`, `voicetype_local/config.py` — глобальная клавиша;
- `voicetype_local/ui.py`, `voicetype_local/settings_ui.py` — overlay/focus/a11y;
- `voicetype_local/focus.py`, `voicetype_local/inserter.py` — безопасная вставка;
- `voicetype_local/diagnostics.py` — логи;
- `voicetype_local/secrets.py`, `voicetype_local/providers.py`,
  `voicetype_local/correction.py`, `voicetype_local/cloud_transcriber.py` —
  облако и секреты;
- `voicetype_local/memory.py`, `voicetype_local/audio.py` — локальные данные;
- `requirements.txt`, `VoiceType Local.spec`, `THIRD_PARTY_NOTICES.md`,
  `SECURITY.md`, `PRIVACY.md` — поставка и обещания продукта.

Вне доказанного охвата: поведение стороннего пакета `uiautomation` внутри его
реализации, Windows UIPI/secure desktop в реальной системе, malware/advisory
status зависимостей, подпись бинарника, installer/update channel и Git history.

## 3. Подтверждённые защитные меры

### 3.1. Закрытая граница голосовых команд

- В режиме `dictation` parser не создаёт Windows-action.
- В `mixed` команда требует точный префикс `команда` / `command` / `comando`.
- Грамматики привязаны к началу/концу строки; свободного LLM-agent fallback нет.
- Явно запрещены shell/terminal/admin/UAC/registry/shutdown-команды, URL, UNC и
  drive paths, `.exe/.bat/.cmd/.ps1`, pipes, redirects и shell substitutions.
- `VoiceControlRouter` передаёт в executor только `ControlRequest`; исходная
  фраза команды не сохраняется в control route и не попадает в диагностику.

Ограничение: вычисленный parser-ом `CommandRisk` находится в `VoiceRoute`, но
не входит в `ControlRequest` и не участвует в решении executor-а. Это является
частью blocker B1.

### 3.2. Типизированная граница Windows Control

`ControlRequest.from_action()` принимает только фиксированные intents и поля:
app id, имя/номер элемента, snapshot id, направление/число прокрутки и закрытые
клавиши. В схеме нет executable path, command line, URL, текста для набора,
shell-режима или elevation-флага. Неизвестные поля отклоняются, строки и
комбинации ограничены по длине.

### 3.3. Запуск приложений

- Текущий default registry состоит из фиксированных приложений.
- Путь должен быть абсолютным; известные terminal/admin executables запрещены.
- Голос не может предоставить путь или аргументы.
- Запуск использует массив аргументов, `shell=False` и закрытые stdio.

Ограничения доверия к пути и elevated inheritance вынесены в B4 и R4.

### 3.4. Win32 target checks и клавиши

- Перед window/scroll/key/hotkey действиями Win32 backend получает PID и имя
  foreground/target process.
- Неизвестная identity, terminal/admin basename, elevated target и ошибка
  определения elevation блокируются.
- Отправляются только клавиши и сочетания из закрытых allowlists.
- `Win+R`, `Win+X`, `Ctrl+Alt+Delete` и произвольные сочетания не поддерживаются.

Ограничение: проверка окна и глобальный `SendInput` не атомарны; foreground может
смениться между проверкой и вводом. Подтверждение также не связывает клавишу с
исходным окном. См. B2 и B5.

### 3.5. UI Automation: минимизация данных и stale checks

- Adapter запрашивает только `ControlTypeName`, `Name`, `IsEnabled`,
  `IsOffscreen`, `BoundingRectangle` и runtime id.
- `ValuePattern`, текст edit/document control, пароль, screenshot, clipboard и
  window title не запрашиваются.
- Роли ограничены button/checkbox/combo/hyperlink/list/menu/radio/tab;
  имя ограничено 120 символами, роль — 64, число элементов — максимум 200.
- Используется per-thread `UIAutomationInitializerInThread`.
- Между потоками сохраняются только immutable descriptors; живые COM controls
  заново перечисляются внутри текущего UIA thread.
- Numbered invoke требует последний snapshot id, тот же foreground root и
  совпадение number/name/role/bounds/runtime id; enabled проверяется повторно.
- Ошибки UIA закрываются отказом, а не разрешением действия.

Ограничения: accessible `Name` сам может быть содержимым страницы; при отсутствии
runtime id идентичность слабее; TTL и очистка реализованы не на всех слоях.
См. B2, B7 и R2.

### 3.6. Базовая механика подтверждения

- Без trusted UI policy используется `DenyConfirmationPolicy`.
- Pending approval связан с fingerprint typed request.
- `confirmed=True` не является общим bypass: он принимается только после
  совпадающего `confirmation_required`.
- Mismatch уничтожает старое approval; one-shot потребляется до попытки действия.
- App устанавливает логический deadline 45 секунд.

Этого недостаточно для связи с реальным desktop target и backend-level TTL —
см. B2.

### 3.7. Диагностика без содержимого

- `TechnicalLogger` принимает только фиксированные events и поля.
- В allowlist нет transcript, element name, target label, path, window title,
  exception message или API key.
- Ошибки пишутся как class name; значения ограничены 160 символами и очищаются
  от переводов строки.
- Ротация ограничена 512 KiB и тремя backup-файлами.
- Просмотренные call sites передают canonical operation/reason/backend/duration,
  а не текст речи или UIA names.

### 3.8. Аудио, текущий текст и локальная память

- Запись собирается в RAM и возвращается как `BytesIO`; текущий recorder не
  создаёт WAV-файл на диске.
- Audio buffer закрывается после распознавания/ошибки/cancel path.
- Raw/local/corrected text хранится в RAM текущей сессии, может быть явно очищен
  и обнуляется при штатном выходе.
- Voice command transcript не добавляется в last-text session state.
- SQLite memory не имеет таблицы transcript/audio; она хранит явные terms,
  aliases, scopes, styles и packs.

Уточнение: для применённых словарных замен сохраняются `use_count` и
`last_used_at`. Это не полный transcript, но это производная история
использования термина и её нужно явно раскрыть или отключить. См. R7.

### 3.9. Облачные режимы и API secret

- Default transcription локальный; cloud text и cloud audio имеют отдельные
  consent flags.
- Consent принимает только настоящий JSON boolean; malformed value возвращает
  безопасный default `False`.
- Cloud provider не строится без соответствующего consent/model/key.
- OpenAI endpoints фиксированы на официальных HTTPS URL; произвольный endpoint
  из settings не принимается.
- Text correction отправляет текст, но не аудио; cloud transcription отправляет
  аудио, но не приватные memory hotwords.
- API key отсутствует в settings/model changes/logs, поле UI маскировано.
- На диске ключ защищён DPAPI текущего Windows logon и пишется atomic replace.
- Ошибки provider-а превращаются в общие сообщения без тела ответа и ключа.

Транзакционность settings/secret пока недостаточна — см. B6.

### 3.10. Overlay и глобальная activation key

- Status overlay имеет `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` и показывается с
  `SW_SHOWNOACTIVATE`.
- Number overlay дополнительно имеет `WS_EX_TRANSPARENT`, click-through и рисует
  только номера, не accessible names.
- Глобальный hook подавляет только захваченную primary/dedicated клавишу;
  callback быстро ставит действие в app queue, не выполняя Tk работу в hook.
- Невалидная activation pair сбрасывается в `right_ctrl + toggle`.
- Win-modifier сочетания намеренно не разрешены, поскольку его безопасное
  подавление текущим hook не гарантируется.

Runtime/a11y проверки оверлея и hook всё равно обязательны — см. R5 и R6.

## 4. Release blockers

### B1 — UIA и `Space` обходят политику опасных действий

Severity: **High**

Статическое основание:

- parser знает слова `confirm`, `authorize`, `share`, `publish`, `post`,
  `sign in/out`, `order`, `checkout` и переводы, но `CommandRisk` не доходит до
  executor;
- `_looks_destructive()` в UIA использует более короткий список и не считает
  многие такие элементы опасными;
- numbered click вообще не содержит target text из произнесённой команды;
- `HyperlinkControl` и `MenuItemControl` допускаются к Invoke/Click. Они способны
  открыть URL/custom protocol/download, запустить IDE task/terminal или вызвать
  admin/UAC action, хотя parser не принимал URL, shell или elevation;
- название вроде `Continue`, иконка без явного destructive слова или локализация
  не позволяют надёжно понять эффект по substring;
- `SPACE` находится в safe keys, хотя Space активирует focused button/checkbox и
  может отправить, удалить или подтвердить действие.

Требуемое исправление:

1. Executor должен независимо от parser-а применять единую RU/ES/EN политику
   forbidden/sensitive/destructive к **фактическому** UIA element.
2. Terminal/admin/UAC/run/execute/open-external действия должны блокироваться
   до Invoke, в том числе при выборе по номеру.
3. Неизвестные MenuItem/Hyperlink и другие потенциально внешние действия должны
   по умолчанию требовать подтверждение; безопасные действия задаются закрытой
   матрицей role + semantic action.
4. Parser risk можно передать как дополнительный сигнал, но он не должен быть
   единственной защитой.
5. `SPACE` нужно убрать из immediate-safe либо привязать к проверенному focused
   target и требовать подтверждение.
6. Confirmation UI должен показывать bounded summary: действие, приложение/окно
   и элемент, не записывая эти данные в лог.

Условие снятия blocker-а: negative tests доказывают, что `Confirm`, `Authorize`,
`Share`, `Publish`, login/logout, order/checkout и переводы не выполняются первой
фразой; numbered `Open Terminal`, `Run as administrator`, Delete и hyperlink не
обходят policy; Space на Send/Delete не выполняется сразу.

### B2 — подтверждение не связано с исходным desktop target и не имеет полного TTL

Severity: **High**

Статическое основание:

- Для named element первый запрос создаёт snapshot A и challenge
  `element:<number>`. При подтверждении named path делает новый capture B.
  Fingerprint содержит intent/name, но не исходные window id/runtime id/bounds;
  одноимённый destructive control с тем же номером в другом окне может получить
  approval от A.
- Key/hotkey fingerprint содержит только команду клавиш, но не PID/HWND/focused
  element. Пользователь может переключить окно в 45-секундном интервале.
- TTL 45 секунд проверяется app только при попытке подтверждения. Executor сам
  не хранит issued-at/deadline, а истёкший pending object не очищается таймером.
- Number snapshot app визуально истекает через 120 секунд, но executor/backend
  не имеют собственного timestamp/TTL.

Требуемое исправление:

- challenge должен включать issued-at/deadline и immutable target binding:
  process identity, foreground/root HWND or UIA window id, snapshot id, element
  runtime id/name/role/bounds; для key/hotkey — точный foreground/focus target;
- confirm должен повторно проверить **тот же** target и не заменять snapshot A
  общей recapture B;
- TTL и one-shot consumption должны находиться в executor/policy boundary, а app
  timer должен физически очищать pending object;
- focus/foreground нужно проверить непосредственно перед side effect;
- при mismatch, focus change, отсутствующем runtime id в опасном действии или
  timeout — только fail-closed.

Условие снятия blocker-а: обязательный regression test A→B (одинаковое имя и
номер, другое окно) получает reject; те же тесты для Enter/Delete/Alt+F4 после
смены foreground; fake clock доказывает отказ после TTL на executor level.

### B3 — cancel не отменяет side effect; control state не сериализован

Severity: **High**

Статическое основание:

- UIA capture/execute запускаются daemon threads без operation deadline и без
  cancellation token, переданного executor/backend.
- Escape/cancel увеличивает app generation и игнорирует поздний result, но уже
  идущий Invoke/Click/SendInput/Popen этим не останавливается.
- После cancel app возвращается в Idle; новый worker может стартовать, пока старый
  ещё жив.
- `WindowsControlExecutor` хранит `_last_snapshot`, `_pending_confirmation`,
  `_active_request_fingerprint` и one-shot state без общего lock.
- UIA backend держит `RLock` вокруг сторонних UIA calls; зависший call способен
  заблокировать следующие UIA operations на неопределённое время.

Требуемое исправление:

- один сериализованный control dispatcher; запрет второго in-flight действия;
- thread-safe executor state и атомарное создание/поглощение confirmation;
- monotonic deadline и cancellation token, проверяемые непосредственно перед
  необратимым side effect;
- UIA isolation с реальным timeout boundary. Обычный Python thread нельзя
  безопасно «убить», поэтому для untrusted/hang-prone UIA может потребоваться
  отдельный worker process либо строго управляемый dedicated COM worker;
- после cancel late worker не должен иметь возможности Invoke/Click/SendInput.

Условие снятия blocker-а: slow/hung fake backend, cancel-before-effect и два
конкурентных request-а не дают позднего действия и не смешивают approvals;
приложение остаётся управляемым после backend timeout.

### B4 — нет общего барьера для elevated запуска VoiceType и UIA target

Severity: **High**

Статическое основание:

- Win32 backend хорошо блокирует elevated/unknown target, но UIA backend не
  выполняет аналогичную PID/elevation/forbidden-process проверку.
- Dictation inserter также не проверяет target elevation/process class.
- В приложении нет startup guard, запрещающего работу самого VoiceType с
  elevated token.
- `subprocess.Popen` наследует token родительского процесса, поэтому разрешённое
  приложение будет запущено elevated, если elevated сам VoiceType.
- В spec нет явного custom privilege manifest; полагаться только на обычный
  способ запуска недостаточно.

Требуемое исправление:

- explicit `asInvoker` manifest и runtime self-token check;
- если VoiceType elevated — Control и automatic insertion должны fail-closed
  (предпочтительно понятный отказ/перезапуск без elevation);
- UIA root PID должен пройти ту же forbidden/elevation policy, что Win32;
- запрещён запуск дочернего процесса при elevated self;
- UAC secure desktop/UIAccess никогда не обходятся.

Условие снятия blocker-а: manual matrix для standard VoiceType → standard app,
standard → elevated app, elevated VoiceType, UAC prompt и secure desktop. Во всех
небезопасных сочетаниях нет чтения/Invoke/SendInput/Popen.

### B5 — вставка текста не обеспечивает заявленную границу shell/URL и target focus

Severity: **High** относительно текущего обязательства в `SECURITY.md`

Статическое основание:

- Dictation по назначению вставляет произвольный распознанный текст в выбранное
  пользователем поле; process denylist для terminal/admin/URL-like targets нет.
- `repeat_last()` вставляет текст в текущий focus без повторной target identity
  проверки.
- Automatic insertion сравнивает focus перед вставкой, но между сравнением и
  chunked `SendInput` есть TOCTOU; во время длинной вставки focus может смениться.
- `\n` и `\t` отправляются как Unicode events, а не physical Enter/Tab. Их
  реальное поведение в CMD, PowerShell, Windows Terminal, browser omnibox и
  нестандартных controls статически не доказано.
- Текущий текст `SECURITY.md` утверждает, что recognized speech никогда не
  передаётся shell/command interpreter/URL launcher. Универсальная диктовка в
  user-focused terminal с этим абсолютным обещанием несовместима.

Требуемое решение продукта и кода:

1. Зафиксировать threat model: либо блокировать automatic/repeat insertion в
   terminal/admin/unknown targets, либо честно сузить обещание до command-plane.
2. До решения — terminal/admin target должен fail-closed; newline/tab там нельзя
   отправлять без отдельного безопасного поведения.
3. Перед каждым insertion chunk проверять тот же focus/process; при изменении
   останавливать ввод и отмечать возможную частичную вставку.
4. По возможности уменьшить окно между final check и одним `SendInput` batch.

Условие снятия blocker-а: manual tests CMD/PowerShell/Windows Terminal/browser
omnibox и focus-switch во время длинного текста; никакой newline/tab не запускает
команду и текст не продолжает уходить в новое окно. Документация соответствует
реальному выбранному threat model.

### B6 — settings/secret update не транзакционен и может нарушить cloud consent

Severity: **High (privacy)**

Статическое основание:

- `SettingsStore.update()` сначала меняет live `_settings`, затем пишет temp
  file. При ошибке записи rollback нет.
- `VoiceTypeApp._settings_saved()` перестраивает cloud providers только после
  успешного возврата `update()`. Поэтому failed save при отключении cloud может
  оставить в store новые local/consent=False значения, а старый cloud provider
  — действующим.
- После ошибки UI показывает общий отказ, но Cancel закрывает окно без возврата
  уже мутировавшего store.
- API key сохраняется до settings update; если settings save упал, DPAPI secret
  уже может остаться на диске.
- `_load()` предполагает JSON object. Валидный JSON верхнего уровня `[]`, строка
  или число вызывает не перехваченную ошибку `.items()` и способен сорвать
  запуск вместо безопасного восстановления default settings.

Требуемое исправление:

- собрать immutable candidate, полностью validate, записать/flush/atomic replace
  и только затем заменить live settings;
- на любой ошибке оставить прежний целостный snapshot либо явно rollback;
- отключение cloud/удаление consent/key должно немедленно закрывать runtime
  provider до попытки сетевого запроса, даже если persistence недоступен;
- согласовать secret + settings как явную операцию с понятным partial-failure;
- top-level non-object, oversized/corrupt JSON безопасно quarantine/reset;
- отдельно тестировать disk full, permission denied и failed replace.

Условие снятия blocker-а: fault-injection test при выключении cloud доказывает,
что после failed write ни один новый text/audio request не уходит; corrupted
settings не мешают безопасному запуску и не включают cloud/commands.

### B7 — UIA names хранятся дольше заявленного и могут быть содержимым документа

Severity: **Medium/High (privacy promise)**

Статическое основание:

- `Name` у Hyperlink/ListItem/Combo/Button часто является видимой подписью,
  темой письма, именем контакта, текстом ссылки или выбранным значением. Поэтому
  отсутствие `ValuePattern` не означает отсутствие document content.
- `_hide_number_overlay()` очищает app reference и canvas, но не
  `WindowsControlExecutor._last_snapshot` и не `UiaControlBackend._snapshot`.
- После named click backend тоже сохраняет весь последний набор accessible names.
- Pending confirmation логически просрочено через 45 секунд, но объект не
  очищается запланированным таймером.
- Это расходится с формулировками `PRIVACY.md` об отсутствии document text и
  очистке snapshots/confirmations на selection/cancel/mode change.

Требуемое исправление:

- единый `clear_snapshot()` на app/executor/backend; вызывать после Invoke,
  cancel, mode change, overlay TTL, timeout, error и exit;
- backend-level created-at/TTL и физическая очистка pending confirmation timer;
- хранить минимальный набор/минимальный срок, не показывать и не логировать names;
- пересмотреть необходимость Hyperlink/ListItem по умолчанию;
- обновить privacy text: field values/passwords не читаются, но bounded accessible
  labels могут содержать видимый контент.

Условие снятия blocker-а: sentinel element name исчезает из всех трёх уровней
после hide/invoke/cancel/TTL; sentinel никогда не появляется в log/settings/export.

### B8 — обязательный supply-chain gate для `uiautomation`

Severity: **release gate; уязвимость пакета этим аудитом не установлена**

Подтверждено:

- runtime pin: `uiautomation==2.0.29`;
- packaged self-test проверяет import, symbols и metadata version;
- `THIRD_PARTY_NOTICES.md` указывает upstream и Apache-2.0;
- spec включает пакет через `collect_all()`.

Не подтверждено:

- hashes wheel/sdist, provenance и точный артефакт сборки;
- advisory/malware scan и ручной review reachable code;
- состав того, что `collect_all()` фактически положил в дистрибутив;
- SBOM, reproducibility и подпись готового exe/installer.

Условие снятия gate: hash-locked dependencies, SBOM, vulnerability/license scan,
проверка upstream/release artifact, минимизация bundled modules/data, secret scan
готовой папки и подпись опубликованного артефакта. Import-only self-test этого не
заменяет.

## 5. Открытые риски и обязательные manual checks

### R1. Runtime id и HWND reuse

Если UIA runtime id отсутствует, element identity допускает `None == None`, а
root identity откатывается к HWND + control type. После замены control или reuse
HWND одинаковые name/role/bounds/number могут совпасть. Для опасных действий
нужен fail-closed при слабой identity либо дополнительная process/window identity
и короткий backend TTL.

### R2. Accessible labels и минимизация

Проверить Chrome/Edge, Explorer, Office, VS Code и системные диалоги: какие Names
возвращают Hyperlink/ListItem/Combo, нет ли email subjects, filenames, contacts,
selected values и ARIA content. Использовать синтетические sentinel данные.

### R3. Process/path trust для application registry

- Default paths строятся из `WINDIR`, `ProgramFiles*`, `LOCALAPPDATA`.
- Installed candidate проверяется как file, но не по trusted root/signature/hash.
- Switch ищет окно по executable basename, а не полному canonical image path.
- Registry arguments считаются trusted и сейчас пусты; их нельзя в будущем
  загружать из profile/settings/memory pack.

Перед публичным выпуском использовать Known Folder APIs/canonical path, сравнивать
полный image path, зафиксировать registry как internal trusted data и оценить
signature policy для системных/установленных приложений.

### R4. Native focus TOCTOU

Даже после `_safe_window()` глобальный foreground может смениться до `SendInput`.
Проверить aggressive focus stealing, Alt+Tab, notification popups и application
switch. Sensitive actions должны повторно сверять bound target сразу перед input.

### R5. Hotkey suppression и потерянный key-up

Проверить физические клавиатуры, on-screen keyboard, Sticky/Filter Keys, RDP,
lock/unlock, UAC transition и layout changes:

- right Ctrl press/release полностью подавляется только как выбранная dedicated
  key;
- auto-repeat не создаёт повторных toggles;
- потерянный key-up не оставляет detector в held state;
- hold mode гарантированно заканчивает запись;
- Pause/Break имеет надёжный key-up либо разрешён только в toggle;
- Alt+F-key binding не оставляет Windows/app menu активированным из-за прошедшего
  modifier press/release;
- Escape подавляется только когда cancel действительно доступен.

### R6. Overlay/settings accessibility

Статически overlay не забирает focus, но необходимы реальные проверки:

- Narrator и NVDA объявляют Recording/Processing/Error/Confirmation, не меняя
  курсор пользователя;
- confirmation сообщает точное действие и target, но не пишет его в лог;
- Settings полностью доступны Tab/Shift+Tab/arrow/Space/Escape без мыши;
- preceding ttk Label действительно даёт control accessible name;
- глобальный `<Return>` не сохраняет/закрывает окно неожиданно при активации
  focused button/combobox;
- Number overlay видим при 125/150/200% DPI, нескольких мониторах и negative
  coordinates; его фиксированный 11pt номер масштабируется с a11y size setting;
- high contrast и extra-large режимы проверены не только визуально, но и с
  Windows High Contrast.

### R7. «Нет истории» — точная формулировка

Полной ленты диктовок нет. Но продукт всё же хранит:

- текущий raw/local/corrected text до clear/exit;
- явный vocabulary/aliases/styles;
- `use_count` и `last_used_at` применённых терминов;
- backup memory DB при миграциях;
- DPAPI secret, settings и bounded technical logs;
- clipboard после явного Copy находится под контролем Windows/пользователя.

`PRIVACY.md` должен называть term usage metadata и backups. Нужен выбор: не
хранить last-used, хранить coarse statistic без timestamp или дать понятный
переключатель/reset/export disclosure.

### R8. Ошибки, crash state и session memory

UI показывает некоторые raw exception strings для microphone/transcription.
Они не попадают в logger, но могут раскрыть device/path details на экране.
Рекомендуется bounded error-code mapping. Также отдельно описать, что OS crash
dumps/clipboard находятся вне гарантии «не хранить в файлах приложения».

### R9. Голосовое подтверждение как тот же канал

Опасная команда и `команда подтверждаю` распознаются тем же микрофоном без speaker
authentication. Hotkey делает сценарий намеренным, но nearby/system audio всё
равно следует проверить. Для high-impact action полезны target-specific phrase
и ясное визуальное/доступное описание; generic «подтверждаю» не должно работать
после смены окна или для другого действия.

## 6. Карта данных и фактическое хранение

| Данные | Где | Срок сейчас | Оценка |
|---|---|---|---|
| Microphone PCM/WAV | RAM frames + `BytesIO` | одна запись/распознавание | Хорошо; проверить crash/timeout paths динамически |
| Raw/local/corrected transcript | `last_*` в RAM | сессия, clear или exit | Не persistent history; чувствительные данные доступны из tray до clear |
| Voice command phrase | parser/route в RAM | обработка команды | Не добавляется в session text/log |
| UIA accessible names | app + executor + UIA snapshot | app overlay до 120 s, нижние слои до next capture/exit | Исправить B7 |
| Pending action/element name | app + executor | deadline 45 s, объект до cancel/next action/exit | Добавить physical timer clear и executor TTL |
| Vocabulary/styles | `memory.sqlite3` и migration backups | persistent до удаления | Ожидаемо, пользовательские чувствительные данные |
| Term usage metadata | `use_count`, `last_used_at` | persistent | Раскрыть/минимизировать |
| Settings/consent | `settings.json` | persistent | API key отсутствует; исправить B6 |
| API key | `secrets/*.dpapi` | до удаления | DPAPI current-user; plaintext существует в process RAM при работе provider |
| Technical diagnostics | rotating log | active + 3 backups | Allowlisted, без transcript/UIA names по текущим call sites |
| Clipboard copy | Windows clipboard | до замены/очистки пользователем/OS | Только явное действие, вне history VoiceType |
| Cloud payload | provider | по отдельному consent | Условия/retention provider применяются отдельно |

## 7. Приоритет исправлений

### P0 — до любого публичного Control preview

1. Закрыть B1: единая фактическая policy UIA/keys, запрещённые indirect actions.
2. Закрыть B2: target-bound, executor-TTL, one-shot confirmation; test A→B.
3. Закрыть B3: сериализация, cancellation/deadline, отсутствие late side effects.
4. Закрыть B4: self-elevation и UIA/process integrity barrier.
5. Закрыть B6: transactional/fail-closed cloud settings.
6. Очистить snapshots/pending names и исправить privacy wording (B7).

### P1 — до объявления security/privacy guarantees

1. Решить B5 и согласовать universal dictation с shell/URL threat model.
2. Выполнить dependency/artifact gate B8.
3. Пройти hotkey, focus-race, DPI/multi-monitor и screen-reader manual matrix.
4. Добавить sentinel privacy tests для logs/settings/export/snapshots.

### P2 — hardening после безопасного preview

1. Trusted path/signature hardening application registry.
2. Минимизировать UIA roles и packaged modules.
3. Управление term usage metadata и session-text auto-clear/lock behavior.
4. Формализовать threat model для nearby/system-audio voice injection.

## 8. Минимальная release acceptance matrix

Release gate считается закрытым только при наличии сохранённого результата:

1. **Parser/executor negative suite:** shell, URL, path, UAC, admin, terminal,
   actual UIA sensitive names и numbered bypass на RU/ES/EN.
2. **Confirmation suite:** exact target, one-shot, mismatch, TTL, A→B, foreground
   switch, concurrent request и replay.
3. **Cancellation suite:** slow/hung backend, Escape до side effect, no late Invoke.
4. **Integrity suite:** standard/elevated VoiceType и target, UAC secure desktop.
5. **Privacy sentinel suite:** уникальная phrase/name/key отсутствуют в logs,
   settings, exports и snapshots после clear/TTL.
6. **Insertion suite:** focus switch mid-text, terminal/newline/tab, browser omnibox,
   partial SendInput and repeat-last.
7. **A11y manual suite:** right Ctrl toggle/hold, Pause policy, Sticky Keys, RDP,
   NVDA/Narrator, keyboard-only settings, DPI/multi-monitor/high contrast.
8. **Supply chain:** hash lock, SBOM, advisories/licenses, packaged contents,
   secret scan, antivirus/reputation check и code signing.

## 9. Решение аудита

Текущую архитектуру не нужно выбрасывать: typed intents, закрытые allowlists,
DPAPI, bounded logs, memory-only audio и immutable UIA descriptors — правильная
основа. Но UIA является не просто «кликом», а косвенным capability layer, который
может открыть URL, запустить действие приложения, отправить данные или вызвать
UAC. Поэтому безопасность должна приниматься по **фактическому target и effect**,
а подтверждение — быть короткоживущим и привязанным к исходному окну/элементу.

До выполнения P0 VoiceType Control допустим только как внутренний эксперимент с
нейтральными данными, не как публично заявленная безопасная функция управления
Windows. После исправлений нужен повторный статический review и отдельный
динамический Windows acceptance run; этот документ сам по себе не является
сертификатом безопасности.
