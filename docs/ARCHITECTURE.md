# VoiceType Local architecture

```text
Right Ctrl / configured switch
  -> non-activating state machine
  -> in-memory microphone recording
  -> supervised Whisper worker process
  -> deterministic vocabulary and safe normalization
  -> optional text corrector with protected-token validation
  -> focused-field verification
  -> Unicode SendInput or pending insertion
```

## Process boundaries

The UI/main process owns the microphone buffer, SQLite MemoryStore, focused-field
snapshot, insertion, consent, and secrets. CTranslate2/faster-whisper runs in a
separate supervised process. If inference hangs, the parent terminates that
process, creates a fresh one, and retries the same in-memory WAV at most once.

The Whisper worker never opens the memory database and never receives the full
profile. It receives only the current language and a bounded prompt/hotword
slice.

## Storage boundaries

- settings: small atomic JSON;
- durable product memory: local SQLite with migrations and backup;
- packs/import/export: validated small JSON/CSV data;
- secrets: current-user Windows DPAPI;
- audio/transcript: RAM only;
- diagnostics: allowlisted, rotating, no content.

Vector search is intentionally absent. Exact aliases and deterministic scope
precedence are safer for short vocabulary. Optional FTS5 is a derived index with
a normal-index fallback.

## Failure policy

Every optional feature fails closed to the local transcript. A network error,
missing key/model, invalid AI schema, focus mismatch, protected-token change, or
worker crash must never silently discard or insert altered content.
