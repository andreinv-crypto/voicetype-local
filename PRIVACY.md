# VoiceType Local — privacy

## Default local mode

VoiceType Local is designed to work without an account or internet connection.
In the default configuration:

- microphone audio is kept in memory only for the current dictation;
- the resulting transcript is kept in memory only for insertion/retry;
- audio and transcripts are not written to diagnostics;
- no field values, screenshots, clipboard history, document bodies or
  background edits are deliberately collected;
- voice-command source text is not added to session transcript memory or
  diagnostics;
- the local memory database stores terms/preferences explicitly added or
  imported by the user and metadata for bundled packs; bundled packs are
  disabled until the user explicitly enables them;
- technical diagnostics are bounded rotating logs containing only state,
  duration, backend, version, and error type.

## Optional Windows voice control

VoiceType Control uses Microsoft UI Automation only after an explicit command
such as showing numbered controls or clicking an accessible element. It reads a
bounded accessible element name, control role, enabled/off-screen state,
geometry and opaque runtime identifier. It does not request field values,
document text, passwords, screenshots or clipboard contents.

An accessible `Name` is supplied by the active application and can itself be a
visible label, link text, contact name or document-related title. VoiceType
keeps it only in the bounded temporary snapshot needed for the explicit Control
action, never writes it to diagnostics/settings/history, and clears the
snapshot after use, cancellation or expiry.

Numbered element snapshots and pending confirmations are temporary in-memory
objects. They expire or are cleared on selection, cancellation, mode change and
application exit. Logs record only an allowlisted command intent, result code,
backend and duration — never the spoken command, element name, window/document
title or field content.

The application does not contain a persistent dictation-history feed. Raw,
dictionary-normalized and corrected text from the current session can be
copied or cleared by the user and is released on exit.

## Optional cloud text correction

This mode is off by default. It requires explicit consent, a provider/model, and
a user-supplied API key. Only the draft text and a small relevant vocabulary
slice are sent. Audio is never sent by the text-correction mode.

For OpenAI, VoiceType requests `store: false`, but this setting alone is not the
same as an organization enrolled in Zero Data Retention. The provider's current
terms and data controls still apply.

## Optional cloud transcription

This separate mode is off by default and requires separate consent because it
sends microphone audio to the selected provider. It is never enabled merely by
enabling text correction. Private memory terms/hotwords are not included in the
cloud transcription request; they remain available to the local fallback.

## Local files

User-owned files live under `%LOCALAPPDATA%\VoiceTypeLocal`:

- `settings.json` — application settings and consent flags, never API keys;
- `data\memory.sqlite3` — explicit vocabulary, aliases, scopes, and styles;
- `secrets\*.dpapi` — Windows DPAPI-protected provider secrets;
- `logs\voicetype.log*` — bounded technical diagnostics.

The user can delete the memory database to reset learned vocabulary. A clean
database is created automatically. Exported profiles never include audio,
transcripts, logs, or secrets.

Memory exports are intentionally portable, unencrypted JSON files. They can
contain private vocabulary; store/share them like a sensitive document and
delete unnecessary copies explicitly.

## Feedback

VoiceType does not upload diagnostics automatically. A future report/export
action must show exactly what will be shared and require an explicit user action.
