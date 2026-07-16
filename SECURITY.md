# Security policy

VoiceType Local handles microphone audio and text that may be sensitive. Treat
all changes to capture, insertion, storage, updates, and cloud providers as
security-sensitive.

## Supported development version

Only the latest locally verified candidate and the currently installed stable
version are supported during private development. No public release channel is
active yet.

## Reporting a vulnerability

Do not post real transcripts, audio, API keys, personal data, or exploit details
in a public issue. Until a private security contact is selected, report the
problem directly to the repository owner and include only sanitized reproduction
steps.

## Required controls

- Never commit `.env`, API keys, credentials, audio, transcripts, user profiles,
  databases, or logs.
- Provider secrets must use Windows Credential Manager or DPAPI.
- Memory/import content is untrusted data and must not become AI instructions.
- Cloud features require explicit consent and a local fallback.
- Updates are staged, self-tested, and reversible before shortcuts are changed.
- Logs must use an allowlist of technical fields and bounded rotation.
- Dependencies and packaged artifacts must be audited before a release.
- External memory packs are untrusted until their source/signature is verified;
  importing or enabling them always requires an explicit user action.

Before any public GitHub release, create and scan a clean export or sanitized
history. The private development history must not be assumed publication-safe.
