# Contributing during the public preview

Bug reports and accessibility feedback are welcome. Use neutral examples and
never attach real audio, transcripts, memory exports, API keys, medical data, or
other personal information.

The project is not accepting external code contributions during this preview.
Do not submit a pull request or paste implementation code unless the repository
owner has first requested it and both sides have signed a separate written
contribution agreement. An unsolicited pull request may be closed without
review. Submission does not transfer ownership or grant the Licensor additional
commercial or relicensing rights beyond the repository license and the GitHub
Terms of Service.

VoiceType is accessibility-first. Any requested contribution must preserve a
flow usable with one configurable physical control, with Right Ctrl as the
default. Before proposing an agreed change:

1. Do not add personal names, addresses, recordings, transcripts, profiles, or
   API keys to code, fixtures, documentation, screenshots, or commits.
2. Preserve offline behavior and immediate fallback when optional components
   fail.
3. Add tests for affected state transitions, long key holds, focus changes,
   timeouts, protected tokens, and privacy boundaries.
4. Run `python -m pytest -q`, `python -m pip check`, packaged `--self-test`, and
   the UI smoke test.
5. Explain any new network request, stored field, dependency, permission, or
   accessibility interaction.

For ordinary reproducible bugs, use the repository bug-report form. For a
security issue, follow SECURITY.md and use GitHub private vulnerability
reporting; do not publish exploit details in a normal issue.
