# Codex independent review: Cyber Health uninstaller round 1

Status: **BLOCKED — do not use against real host state or real health data**.

Independent command:

```bash
.venv/bin/python -m unittest tests.test_uninstaller -v
```

Observed result: 14 tests, 1 failure and 1 error.

## Blocking findings

1. **Fail-closed ordering is violated.** `run()` calls `handle_data()` before checking whether the OpenClaw or LaunchAgent registration is foreign/refused. With `--purge-data`, data can be moved/deleted even though ownership verification later fails. Separate planning/validation from execution: inspect and prevalidate every target first; any refusal or inspection uncertainty must produce zero mutations. Only after a complete valid plan may host removal and optional purge execute.

2. **OpenClaw inspection errors are misclassified as “not registered”.** Any non-zero `mcp show` result is currently treated as a clean no-op. Permission failures, invalid config, timeouts, corrupt JSON, incompatible CLI output, and CLI startup failure must be explicit failure/refusal states. Only the exact documented “No MCP server named” case may be treated as absent.

3. **Report leaks host configuration.** `status.details` stores and JSON/text serialization exposes the complete `mcp show` payload, including its `env` map. Never echo raw environment values or unrelated host configuration. Report only sanitized ownership evidence, redacted path facts where necessary, and action/result.

4. **Ownership proof is too weak and uses unsafe substring matching.** A matching `cwd` alone, DB alone, or a string containing the project-root text currently returns true. `/path/project-evil` can match `/path/project`; an unrelated command can claim the same cwd. Require the exact fixed server name `cyber-health`, a recognized Cyber Health command signature (`cyber-health-mcp` or Python `-m cyber_health_mcp`), and at least one exact resolved installation binding (cwd/executable/db). Use `Path` ancestry comparisons, never string containment/prefix checks. Reject symlinked ownership evidence.

5. **The foreign-registration override is under-confirmed.** `--force-foreign-host-mcp` can remove an unrelated server with a single Boolean. Keep the target name fixed to `cyber-health` and require a second exact confirmation token such as `--confirm-foreign-unset UNSET_FOREIGN_CYBER_HEALTH`. Do not allow arbitrary `--openclaw-server-name` targeting. The general `obsidian-memory` plugin must remain unreachable by this CLI.

6. **Symlink checks are defeated during initialization.** User paths are `.resolve()`d before `is_symlink()` checks, losing the original link identity. Preserve raw paths, walk existing ancestors with `lstat`/`is_symlink`, then resolve and enforce containment.

7. **Purge boundary is broader than advertised.** `collect_data_paths()` adds every child under `data/`, including unknown directories, and `validate_purge_candidate()` accepts any path anywhere under the project root. Purge must be an explicit allowlist: exact configured SQLite DB plus exact `-wal`/`-shm`, and documented Cyber Health exports only if explicitly requested. Never recursively delete arbitrary directories or source/config files. Remove `shutil.rmtree` fallback from this flow. Fix prefix-collision logic around DB sidecars.

8. **Real host binaries are used in unit tests.** Tests call the installed real OpenClaw and real `launchctl`; this makes results version-dependent and violates the fake-host requirement. Provide deterministic fake executable(s) inside the temporary fixture or inject a subprocess runner. Verify exact argv/environment and simulate show/unset/absent/error states without touching real host tools. Keep one separate opt-in isolated real OpenClaw probe if useful.

9. **Current tests fail.** Normal removal writes a hand-crafted config that OpenClaw 2026.9 rejects, and the path-escape test proves the validator accepts an outside configured DB. Rewrite tests around fake binaries and make escape/ancestor/symlink/prefix-collision assertions meaningful.

10. **README is missing.** Add exact dry-run/default/purge/foreign-refusal examples and state that source, `.venv`, `dist`, user data, Obsidian Vault, Codex state, and the general `obsidian-memory` plugin are outside normal uninstall scope.

## Required verification

- Targeted uninstaller tests all pass with deterministic fake host tools.
- Add adversarial cases for project-root prefix collision, symlinked root/DB/ancestor, corrupt/permission-denied OpenClaw inspection, malformed JSON, command signature spoofing, raw-env redaction, fail-closed zero-mutation behavior, and unknown files in `data/` remaining untouched.
- Existing 130 tests remain green.
- `compileall`, `uv lock --check`, `uv build`, and an isolated real OpenClaw `show/unset` smoke probe pass.
- No operation during implementation or verification touches the user's actual OpenClaw config, LaunchAgents, database, Vault, Codex configuration, or plugin cache.
