# Codex independent review: Cyber Health uninstaller round 2

Status: **NEEDS FINAL HARDENING**. Functional verification is green (148 tests, compileall, lock check, build, and real-host dry run), but the following security edges remain.

## Required fixes

1. **Sanitize every error path, not only successful `mcp show` data.** `inspect_openclaw()` currently copies `combined_output` into `status.reason` for non-absent CLI failures. A CLI/config error may echo environment values or tokens, and `--json` serializes the reason. Return only exit code plus a fixed safe category; never include raw stdout/stderr. Add an error fixture containing fake secrets and assert they are absent from the entire serialized report, including `reason` and `message`.

2. **Close plan-to-execution TOCTOU windows.** Revalidate the exact data target immediately before every move/unlink; `p.is_file()` follows symlinks and is not sufficient. If a prevalidated file is swapped for a symlink, directory, or different resolved target, abort before mutation. Likewise, immediately re-read/re-verify the exact OpenClaw `cyber-health` entry before `mcp unset`, and re-read/re-verify the LaunchAgent before bootout/unlink. If state changed after planning, fail closed. Add deterministic swap tests.

3. **Do not allow destructive purge when host inspection is unavailable.** Missing OpenClaw binary may be a harmless no-op for the default data-preserving uninstall, but `--purge-data` must refuse when host state cannot be inspected, because an existing registration/runtime cannot be excluded. Add a test proving zero data mutation.

4. **Keep LaunchAgent target fixed.** Remove the public `--launchagent-label` targeting option or require the label to equal the project constant. The general-purpose uninstaller must not be usable to aim at arbitrary labels even if another field happens to reference the project.

5. **Document the second foreign-unset confirmation accurately.** README currently says `--force-foreign-host-mcp` alone bypasses refusal and its argument table omits `--confirm-foreign-unset`. State that both flags and the exact `UNSET_FOREIGN_CYBER_HEALTH` token are required. Prefer describing this as recovery-only.

6. **Trash destination safety.** Before using the project-local `.trash` fallback, reject an existing symlink or symlinked ancestor and verify the resolved destination remains strictly inside the project. Never fall through to an unsafe destination.

## Final verification

- Targeted adversarial tests include secret-bearing CLI errors, post-plan DB symlink swap, post-plan OpenClaw/LaunchAgent replacement, missing host inspector with purge, fixed LaunchAgent label, and local-trash symlink.
- All earlier 148 tests plus the new tests pass.
- compileall, `uv lock --check`, `uv build`, and real OpenClaw isolated probe pass.
- Run only `cyber-health-uninstall --dry-run --json` against actual state; do not execute actual uninstall or purge.
