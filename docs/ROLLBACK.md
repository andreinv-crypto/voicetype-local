# Candidate installation and rollback

The current stable executable is not overwritten by `build.ps1`.

1. `build.ps1` creates `dist_candidate\VoiceType Local` and runs unit,
   packaged, and UI smoke tests.
2. `install_candidate.ps1` re-runs self-test, copies to a staging directory
   under `%LOCALAPPDATA%\Programs`, and verifies it before changing anything.
3. Before changing desktop/startup shortcuts, it writes an exact rollback
   manifest (target, arguments, working directory, icon and shortcut settings).
4. An existing installed directory is moved to `VoiceType Local.previous`.
   On the first migration from the workspace build, the untouched workspace
   executable plus the shortcut manifest are the rollback source.
5. If installation fails, the script restores the previous directory and
   shortcuts. `rollback_install.ps1` supports both update and first-migration
   rollback paths and verifies the previous executable before switching.

Do not run either installation script while VoiceType Local is running. During
private development, changing the installed copy and shortcuts requires an
explicit confirmation after candidate test results are shown.
