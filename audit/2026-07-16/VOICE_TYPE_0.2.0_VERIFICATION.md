# VoiceType Local 0.2.0 candidate verification

Date: 2026-07-16

## Result

The 0.2.0 candidate was built separately at
`dist_candidate\VoiceType Local\VoiceType Local.exe`. The running 0.1.1
executable, desktop shortcut, startup shortcut, and installation were not
changed.

Artifact:

- file version: `0.2.0`;
- packaged folder size: `757,754,379` bytes (about 758 MB, including the
  486 MB multilingual Whisper model);
- SHA-256: `51A23ABF203B3DDDBDE400F0FD26B26F89470DC908644F5E015EB303690A921D`;
- Authenticode: not signed (acceptable only for private testing);
- bundled comtypes test modules: removed;
- bundled privacy/security/license/architecture documents: present.

## Automated verification

- project tests: `149 passed`;
- packaged self-test: passed with a bounded 180-second watchdog;
- isolated packaged UI smoke-test: passed without tray, global hotkey, model
  loading, or access to the user's real memory database;
- PowerShell build/install/rollback syntax: passed;
- dependency consistency (`pip check`): passed;
- dependency vulnerability report: 0 known vulnerabilities in the audited
  runtime requirements (`PIP_AUDIT_0.2.0.json`);
- CycloneDX SBOM: `SBOM_0.2.0.cdx.json`;
- secret-pattern scan: clean;
- neutral evaluation manifest: 24 cases validated without reading audio.

## Reliability and privacy controls verified

- Whisper runs in a persistent killable child process with startup,
  transcription, and transfer timeouts plus one clean retry;
- microphone start/stop remain outside the Tk owner thread, and pending/closing
  native streams remain abortable by the watchdog;
- an unabortable PortAudio discovery/open timeout activates a circuit breaker,
  so repeated attempts cannot accumulate while the native call is stuck;
- cancelled generations cannot fall through to a replacement worker;
- automatic insertion occurs only when the exact focused field is confirmed;
- partial/ambiguous SendInput failures cannot blindly repeat the full text;
- malformed cloud-consent values fail closed;
- cloud audio mode does not send private memory hotwords;
- optional AI correction cannot add, remove, or reorder lexical words;
- API keys use DPAPI and can be removed through a two-step explicit action;
- diagnostics use allowlisted event names and fields and never accept dictated
  content or dictionary entries.

## Memory benchmark

With FTS5 enabled, 10,000 synthetic terms used about 5.5 MB. On this machine,
selection/search/prompt operations were approximately 70–111 ms at the 10,000
term checkpoint. Full measurements are in `MEMORY_BENCHMARK_0.2.0.json`.

## Deliberately not completed yet

- no stable executable or shortcut was replaced;
- `install_candidate.ps1` and `rollback_install.ps1` were syntax/review tested
  but not executed against the real installation;
- no real user dictation was recorded for the candidate yet;
- the executable is unsigned and the private repository is not ready for a
  public push; public release requires code signing/licensing review and a clean
  sanitized export.
