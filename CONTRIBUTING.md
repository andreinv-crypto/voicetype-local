# Contributing to VoiceType Local

VoiceType is accessibility-first. The default flow must remain usable with one
configurable physical control, with Right Ctrl as the default.

Before proposing a change:

1. Do not add personal names, addresses, recordings, transcripts, profiles, or
   API keys to code, fixtures, documentation, screenshots, or commits.
2. Preserve offline behavior and immediate fallback when optional components
   fail.
3. Add tests for state transitions, long key holds, focus changes, timeouts,
   protected tokens, and privacy boundaries affected by the change.
4. Run `python -m pytest -q`, `python -m pip check`, packaged `--self-test`, and
   the UI smoke test.
5. Explain any new network request, stored field, dependency, permission, or
   accessibility interaction.

Public contributions are not accepted until the project owner selects a public
license and release process.
