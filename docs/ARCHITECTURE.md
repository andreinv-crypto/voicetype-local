# VoiceType Local architecture

```text
Right Ctrl / configured switch
  -> non-activating state machine
  -> in-memory microphone recording
  -> supervised Whisper worker process
  -> explicit interaction mode
       dictation -> deterministic vocabulary and safe normalization
                   -> optional text corrector with protected-token validation
                   -> focused-field verification
                   -> Unicode SendInput or pending insertion
       commands  -> strict typed RU/ES/EN parser
                   -> confirmation and deny-by-default policy
                   -> allowlisted Windows executor
       mixed     -> exact command prefix? commands : dictation
                   -> exact final Enter/send phrase?
                      insert verified text first
                      -> confirmation -> typed Enter request
```

## Process boundaries

The UI/main process owns the microphone buffer, SQLite MemoryStore, focused-field
snapshot, insertion, consent, and secrets. CTranslate2/faster-whisper runs in a
separate supervised process. If inference hangs, the parent terminates that
process, creates a fresh one, and retries the same in-memory WAV at most once.

The Whisper worker never opens the memory database and never receives the full
profile. It receives only the current language and a bounded prompt/hotword
slice.

Windows control is a separate boundary. The recognized string is never sent to
the shell or treated as executable input. The parser emits a closed typed
intent; the executor validates its arguments and risk policy again. Known
applications are resolved from a fixed allowlist of installed executable
locations. Terminal, administrator/UAC, arbitrary paths and URLs are rejected.

The optional `uiautomation` adapter uses Microsoft UI Automation to enumerate
only actionable element metadata: bounded accessible name, role, enabled state,
geometry and an opaque runtime identifier. It does not read document or field
values. Each call initializes UI Automation in its own worker thread and live
COM controls never cross thread boundaries. A numbered click re-enumerates the
foreground window and compares immutable descriptors before invoking anything;
changed or stale elements fail closed.

## Storage boundaries

- settings: small atomic JSON;
- durable product memory: local SQLite with migrations and backup;
- packs/import/export: validated small JSON/CSV data;
- secrets: current-user Windows DPAPI;
- audio/transcript: RAM only;
- diagnostics: allowlisted, rotating, no content.

The current raw, dictionary-normalized and final transcript stages, plus the
small UI feedback snapshot (`heard -> text -> command -> outcome`), remain only
in process memory and are cleared on exit. Standalone voice-command source text
is routed before these stages and is not retained. A compound everyday-mode
dictation retains only the same session raw/final text already available to the
user; it is never added to diagnostics. There is deliberately no transcript
history store in the current architecture.

Vector search is intentionally absent. Exact aliases and deterministic scope
precedence are safer for short vocabulary. Optional FTS5 is a derived index with
a normal-index fallback.

## Failure policy

Every optional feature fails closed to the local transcript. A network error,
missing key/model, invalid AI schema, focus mismatch, protected-token change, or
worker crash must never silently discard or insert altered content.
