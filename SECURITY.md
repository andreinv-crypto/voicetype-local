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

Before each public release, scan the reachable Git history and the packaged
artifact for secrets, personal paths, runtime data, and unexpected files.
