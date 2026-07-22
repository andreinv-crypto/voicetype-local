# Security policy

VoiceType Local handles microphone audio and text that may be sensitive. Treat
all changes to capture, insertion, storage, updates, and cloud providers as
security-sensitive.

## Supported versions

The latest public preview, `0.2.0-rc1`, receives best-effort security fixes.
Older private or preview builds are not supported. This preview is unsigned and
is not represented as a completed independent security audit.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on the canonical repository:
open **Security** and choose **Report a vulnerability**. Do not post exploit
details in a normal public issue.

Never attach real transcripts, audio, API keys, memory exports, medical data, or
other personal information. Use sanitized reproduction steps and neutral test
data only.

## Required controls

- Never commit `.env`, API keys, credentials, audio, transcripts, user profiles,
  databases, memory exports, or logs.
- Provider secrets must use Windows Credential Manager or DPAPI.
- Memory/import content is untrusted data and must not become AI instructions.
- Cloud features require explicit consent and a local fallback.
- Updates are staged, self-tested, and reversible before shortcuts are changed.
- Logs must use an allowlist of technical fields and bounded rotation.
- Dependencies and packaged artifacts must be audited before a release.
- External memory packs are untrusted until their source/signature is verified;
  importing or enabling them always requires an explicit user action.
- The Windows Control plane must never pass recognized speech to a shell,
  command interpreter, executable path, URL launcher or free-form automation
  agent. It accepts only typed intents and arguments from closed allowlists.
- Dictation is deliberately different: it types text into the field explicitly
  focused by the user. VoiceType does not interpret that text as a command and
  never adds a physical Enter/Tab action. A user who deliberately focuses a
  terminal remains responsible for the text they insert there.
- Terminal, administrator/UAC and secure-desktop operations are denied in the
  Control plane. VoiceType must not attempt privilege escalation or UIAccess
  bypasses.
- Sensitive and destructive UI actions require a short-lived confirmation bound
  to the exact typed request. A stale or different confirmation fails closed.
- Numbered-control UI Automation snapshots contain immutable metadata only.
  Live COM controls stay inside their initialized worker thread; numbered
  invocation must re-enumerate and verify foreground window, identity, role,
  name and bounds.
- Exact Unicode insertion and VoiceType-owned editing in modern Windows 11
  Notepad may additionally hold a bounded snapshot of up to 65,536 UTF-16 code
  units from the focused document in RAM (with one extra unit requested only
  to detect overflow). It is used only for the adjacent
  pre/postcondition, never logged, persisted, hashed or transmitted, and the
  operation fails closed when the bound or identity proof is unavailable.
- Command diagnostics may contain intent/result/backend/duration only; spoken
  text, accessible names, window titles, paths and document content are banned.

Before each public release, scan the reachable Git history and the packaged
artifact for secrets, personal paths, runtime data, and unexpected files.

The implementation status and remaining release gates are tracked in
[docs/IMPLEMENTATION_STATUS_2026-07-17.md](docs/IMPLEMENTATION_STATUS_2026-07-17.md).
