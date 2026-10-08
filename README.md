# Cyber Health Agent (Core & stdio MCP v0.6.4)

> **Breaking upgrade (v0.4.2+):** The MCP interface now uses one internal `owner` identity and no longer accepts `user_id`. Before upgrading an existing installation, back up its SQLite database and complete a verified migration using `cyber-health migrate-owner`. The MCP server refuses to start against an unmigrated database rather than silently hiding facts. Do not run `cyber-health update` on a legacy database until the migration is complete.

> **v0.5.0:** MCP tool arguments are unchanged. The Python `CyberHealthService` API no longer accepts `user_id` either, the service itself (not only the MCP server) refuses an unmigrated legacy database, and the SQLite schema version is now recorded in `PRAGMA user_version`, so a database opened by v0.5.0 must not be downgraded to an older release.

> Independent, pluggable deterministic health engine and stdio MCP server for AI hosts (Codex, OpenClaw, Hermes, etc.). It is not an Obsidian plugin.
> **Current Status**: Core P0 implementation and extended domain capabilities (29 tools total: 7 P0 + 22 extended), including first-run intake, nightly fact collection, target-gap analysis, host automation declarations, next-day plan generation, cross-session wearable screenshot retention, and read-only active-memory pattern suggestions.

> **Agent installation entry point**: For a normal user, follow the single
> [Agent onboarding guide](docs/agent-onboarding.md). It defines the required confirmations,
> Vault isolation, host-specific limits, and truthful failure language.

---

## Architecture Overview

```text
[User Dialogue / Photo Food Description]
                  ↓
[AI Host: Codex / OpenClaw / Hermes]  (Natural language comprehension & vision)
                  ↓ stdio MCP protocol
[Cyber Health MCP Adapter]    (cyber_health_mcp: FastMCP, JSON schema validation, sanitized envelope)
                  ↓
[Cyber Health Domain Core]    (cyber_health: Transactions, safety invariants, revisions, lease state machine)
        ┌─────────┴────────────────────────┐
        ↓                                  ↓
[SQLite Fact Store] (WAL, single fact source)  [MemoryProvider] (Outbox queue / Obsidian)
```

- **Host-Neutral & Headless**: AI hosts do not own health state. Health records are stored in SQLite facts tables with optimistic concurrency (`state_version`).
- **Single-Person MCP Boundary (v0.4.2)**: The MCP tools no longer accept `user_id`; all sessions and hosts on one installation use the internal `owner` identity. Existing databases with other identity partitions require an explicit, verified migration before the new server starts. The Core service API has no identity parameter either: every fact is stored under the fixed `owner` partition key, and a database that still holds other identities is refused by both the MCP server and the service until it is migrated.
- **Strict Read-Only Purity & Consistent Snapshots**: `cyber_health_get_profile` and `cyber_health_get_today` are strictly pure snapshot queries and never insert or mutate database records. `get_today` uses explicit snapshot read transactions (`BEGIN` ... `COMMIT`).
- **Idempotency & Concurrency**: All state-mutating operations strictly require a non-empty `idempotency_key`; hosts should make each key unique per fact (`<tool>-<local date>-<random suffix>`), because all sessions share one key space. Identical retries return the cached response; a different request with a key used in the last 24 hours raises `IDEMPOTENCY_MISMATCH` (nothing is written), while an older key is retired in the audit log and reused. `daily_review` and `plan_tomorrow` replay only while no other write has happened, so a date-stable key never returns a stale review. Stale writes raise `CONFLICT_VERSION`.
- **Timezone Awareness & Real DST Calculations**: Meal times and daily records are converted to the user's timezone (`Asia/Shanghai` default) using standard IANA `zoneinfo`. Daily reminders calculate true local offsets dynamically (e.g. America/New_York `-04:00` / `-05:00`).
- **Missing Data Distinction**: Days without entries are explicitly marked `data_status: "unrecorded"` and `missing_data: true`, distinguishing lack of data from fasting or zero intake. Unconfigured calorie/protein targets return `None` with `status: "unconfigured"`.
- **Cross-Session Screenshot Recall**: `cyber_health_log_workout` persists the user-confirmed structured result of a wearable screenshot (duration, distance, active/total calories, average heart rate, pace, exertion) and can retain its original PNG/JPEG/WebP bytes. The image and its SHA-256 are attached to the same workout fact and included in `export_data`; observed exercise calories are never used to silently increase a food-calorie target.
- **Memory Boundary**: [`obsidian-memory-plugin`](https://github.com/annual30k/obsidian-memory-plugin) is a separate public host extension, not an MCP server, database, or callable MemoryProvider. OpenClaw receives a verified published Release; Hermes uses the plugin's own native installer. Cyber Health's `ObsidianMemoryProvider` is the adapter layer: after validating the selected Vault/project boundary, it connects Core memory intents and queries to `Vault/20-Projects/<projectId>`.
- **Centralized Safety Decision Engine**: A unified evaluation engine (`SafetyRecoveryEvaluation`) enforces strict safety hierarchy across plan generation, prescription, progression suggestion, and progression confirmation:
  1. `SAFETY_RESTRICTED`: Acute red flags (chest pain, syncope, dyspnea) block all workouts and prescribe emergency triage.
  2. `RECOVERY_FLAG_CLEAR_01`: Mandatory 7-day protective deload ($\le 50-60\%$ load, RIR $\ge 3$) after medical clearance.
  3. `TRAIN_RECOVERY_01`: Sleep $< 6.0$h, fatigue $\ge 7$, or recovery score $< 60$ triggers $20-30\%$ volume reduction. Incremental metric submissions merge with previous daily state, preserving prior sleep facts.
  4. `TRAIN_PROGRESSION_STANDARD`: Double progression state machine requiring 2 consecutive sessions at top rep bracket with RPE $\le 8$, load comparability, and proposal signature verification.
- **Concurrency-Safe Outbox State Machine**:
  - **Collision-Free Intent IDs**: Uses canonical tuple JSON SHA-256 (`sha256(json.dumps([user_id, idempotency_key]))`) eliminating separator collisions.
  - **Phase 1 Pre-Reservation**: Persists `operation_log` and `memory_outbox` (`in_flight`) in an atomic transaction before any external IO.
  - **Work-Generation Continuation Keys**: Dynamically hashes the current 50-task batch (`intent_id:attempts:status`) into `maint_{user_id}_{day}_g{hash}`, allowing 51+ task queues and retry backoffs to advance without idempotency blockage.
  - **Lineage-Preserving TTL Pruning**: Safely prunes unreferenced superseded records while preserving parent records and immutable audit logs.
- **Versioned Schema**: SQLite schema changes are ordered, append-only migrations recorded in `PRAGMA user_version`; a database migrated by a newer release is refused rather than opened by older code.
- **Fact Migration & Integrity**: Full safety profiles, revision chains, and schedules are exported and restored idempotently. Duplicate IDs with conflicting data raise `ConflictError` rather than being silently ignored.
- **Evidence-Based Knowledge Retrieval**: Only verified primary literature citations (ISSN, AHA) with valid DOIs/URLs are returned; queries without verified matches return `unavailable` without returning irrelevant items.

---

## Standard Installation Contract (for Agents and Users)

### First installation from GitHub Release

Prerequisites: [uv](https://docs.astral.sh/uv/getting-started/installation/) and at least one supported host CLI (Codex, OpenClaw, or Hermes). Open [the latest Cyber Health Release](https://github.com/annual30k/Cyber-Health-Agent/releases/latest), download its `cyber_health_agent-*-py3-none-any.whl` and `SHA256SUMS` into the same directory, and compare the wheel's SHA-256 with its line in `SHA256SUMS` before running it. The examples below use the `0.6.4` wheel; substitute the wheel name shown on the latest Release when a newer version is published.

macOS/Linux, from the directory containing the downloads:

```sh
# macOS
shasum -a 256 cyber_health_agent-0.6.4-py3-none-any.whl
# Linux: use sha256sum cyber_health_agent-0.6.4-py3-none-any.whl instead
```

```sh
uvx --python 3.11 --from ./cyber_health_agent-0.6.4-py3-none-any.whl cyber-health install --dry-run --json
uvx --python 3.11 --from ./cyber_health_agent-0.6.4-py3-none-any.whl cyber-health install --json
~/.cyber-health/venv/bin/cyber-health status --json
```

Windows PowerShell, from the directory containing the downloads:

```powershell
Get-FileHash .\cyber_health_agent-0.6.4-py3-none-any.whl -Algorithm SHA256
uvx --python 3.11 --from .\cyber_health_agent-0.6.4-py3-none-any.whl cyber-health install --dry-run --json
uvx --python 3.11 --from .\cyber_health_agent-0.6.4-py3-none-any.whl cyber-health install --json
& "$HOME\.cyber-health\venv\Scripts\cyber-health.exe" status --json
```

Compare the displayed hash against `SHA256SUMS`; stop if they differ. Review the dry-run report before applying: an unavailable host CLI is skipped, but a conflicting `cyber-health` registration must be resolved rather than overwritten. `uvx` is only a temporary launcher; `cyber-health install` creates the lasting runtime and local SQLite database under `~/.cyber-health`. Do not use `pip install` or `uvx` alone as the completed installation.

Long-term memory is optional. Only after the user explicitly opts in, has Obsidian installed, and chooses an absolute Vault path, repeat the dry-run and install commands with `--memory-vault "/absolute/path/to/Vault"` (PowerShell: quote the Windows absolute path). OpenClaw's public `obsidian-memory-plugin` is fetched from its verified Release when needed; for Codex or Hermes, install that [independent plugin](https://github.com/annual30k/obsidian-memory-plugin) by its host instructions. Without opt-in, health facts still remain in local SQLite; do not claim Obsidian memory is connected. For the complete consent and failure flow, see the [agent onboarding guide](docs/agent-onboarding.md).

Install and update also link the `cyber-health` command into `~/.local/bin` (Windows: a `cyber-health.cmd` shim in `%USERPROFILE%\.local\bin`; override with `CYBER_HEALTH_USER_BIN_DIR`). An existing file there that Cyber Health did not create is left untouched, shell startup files are never edited, and when the directory is not on PATH the report prints the line to add. Uninstall removes only its own link.

After installation, use the installed `cyber-health update --dry-run --json` and then `cyber-health update --json` to upgrade. For a pre-v0.4.2 database, first complete the backup and identity migration described above. The installer does not silently update at startup.

Update behavior (v0.5.1+):

- `cyber-health update --check` (or `cyber-health status --check-updates`) only reports whether a newer stable Release exists; nothing is changed. Plain `status` never contacts GitHub.
- When the installed version is already the latest Release, `update` exits immediately without a backup or reinstall; `--force` reinstalls and re-verifies anyway.
- After installing a new version, the updater hands the remaining steps (schema verification, host registration checks, metadata) to the newly installed code, so new behavior applies in the same run. If the new version cannot finish, the previous updater completes the verification and says so.
- `cyber-health automation status` (v0.6.4+) compares the host's nightly-review job with this release's spec (schedule and timezone from the profile, target agent, the agent's main conversation, the instruction text and its revision) and lists recent runs with where they were delivered and their token use; `cyber-health status` shows a one-line summary and `update` reports drift. `cyber-health automation sync [--dry-run]` creates or corrects the OpenClaw job: it backs the job up first, never deletes a job, never creates a second one when another host already runs it, and leaves timeouts, models and tool policy alone.
- `cyber-health cleanup [--keep-backups N] [--dry-run] [--json]` (v0.6.3+) runs the same local housekeeping without contacting GitHub, so backup retention and permission repair also work offline or while the API rate limit is exhausted. It only acts on a directory that contains `config/installation.json`.
- On Windows, the updater moves its own locked launchers (`cyber-health*.exe`, e.g. while it runs or while a host keeps the MCP server alive) aside before installing and restores them if the install fails (v0.6.2+). Releases up to 0.6.1 cannot replace their own running `cyber-health.exe`; upgrade those once with `& "$HOME\.cyber-health\venv\Scripts\python.exe" -m cyber_health.update --json`.
- Every successful install/update makes the installation private to the current user (directories `0700`, data files `0600`), keeps the newest 10 rolling update backups (`--keep-backups N`; pre-migration backups are never pruned), and removes orphaned temporary migration files.

When installing Cyber Health for Codex, OpenClaw, or Hermes, the required installation entry point is
`cyber-health install`. Agents must not replace this with only `pip install`, `uv sync`,
or a hand-written host registration command, because those paths do not run the
health-manager long-term-memory preflight.

Run the read-only preflight first, then apply the installation:

```bash
.venv/bin/cyber-health install --dry-run --json
.venv/bin/cyber-health install
```

### Published Core distribution

For end users, Cyber Health Core is distributed as a GitHub Release wheel rather than a source
folder. The installer resolves the newest stable release from
`annual30k/cyber-health-agent`, requires the wheel version to match its tag, verifies GitHub's
SHA-256 digest (or `SHA256SUMS`), and caches it below `~/.cyber-health/releases/`. Normal startup
does not silently update; `cyber-health update` checks for a newer verified Core release. A local
`--project-root` is only a deliberate development override.

The install report contains `memory.state` and `memory.warnings`. Continue with the
SQLite installation when the state is `unconfigured`, but explicitly tell the user what
must be configured before claiming long-term memory is connected. Only `memory.state:
connected` authorizes the installer to add the Obsidian Provider arguments to the
`cyber-health` MCP registration. The same rule applies to `cyber-health update`.

Direct `cyber-health-mcp` launches and manual MCP registration are development or
diagnostic paths only; they do not replace the standard install/update workflow.

## Development-only Direct Execution

The project uses Python 3.12 managed via `uv`. These direct commands are for local
development or diagnostics; they do not perform the long-term-memory preflight.
For a user installation, follow [Standard Installation Contract](#standard-installation-contract-for-agents-and-users).

```bash
# 1. Sync dependencies into local .venv
uv sync --python 3.12

# 2. Run standard P0 stdio server (7 P0 tools, including profile onboarding)
.venv/bin/cyber-health-mcp --db ./data/cyber-health.sqlite3

# 3. Run extended server exposing all 29 verified domain tools
.venv/bin/cyber-health-mcp --db ./data/cyber-health.sqlite3 --allow-all

# 4. Dry-run host integration inspection & uninstallation report
.venv/bin/cyber-health-uninstall --dry-run

# 5. Production-safe uninstallation (unregisters owned host integration; preserves all data)
.venv/bin/cyber-health-uninstall
```

Alternatively, invoke via Python module:
```bash
.venv/bin/python -m cyber_health_mcp --db ./data/cyber-health.sqlite3 [--allow-all]
.venv/bin/python -m cyber_health.uninstall [--dry-run]
```

The production-safe management CLI is the only supported production entry point for the
install/update workflow. Its report includes Codex, OpenClaw, Hermes, and `health-manager` preflight
described below:

```bash
# Inspect without mutating the installation or host configuration
.venv/bin/cyber-health install --dry-run --json
.venv/bin/cyber-health update --dry-run --json

# Apply the install or update; if memory.state is unconfigured, show the warning and remediation
# to the user instead of claiming the long-term-memory bridge is active.
.venv/bin/cyber-health install
.venv/bin/cyber-health update
```

### Codex native MCP registration

When the Codex CLI is available, install and update register the fixed `cyber-health`
stdio MCP entry in Codex's shared local MCP configuration. The command points to the
isolated `~/.cyber-health/venv/bin/cyber-health-mcp` executable and SQLite database.
`cyber-health status` verifies both its command signature and installation path.
Uninstall removes the entry only after the same ownership checks and a last-moment
state fingerprint check. A foreign or ambiguous same-name entry fails closed; all other
Codex MCP entries and settings remain untouched. Use `--skip-codex` only when deliberately
installing without Codex integration.

### Hermes native MCP registration

When the Hermes CLI is available, install and update automatically manage the fixed
`cyber-health` entry through Hermes' native `hermes mcp add/list/test/remove` lifecycle.
The registration points to the same isolated runtime and SQLite fact store used by the other
hosts, then runs a real connection probe and requires discovery of the Cyber Health health-check
and profile tools before reporting success. A disabled owned entry is re-enabled; a foreign or
ambiguous same-name entry fails closed. Uninstall removes only the verified owned entry and leaves
all other Hermes configuration untouched. Use `--skip-hermes` only for an intentional host opt-out.

### Obsidian Memory preflight and host adapters

Every Cyber Health install or update with a user-confirmed `--memory-vault` first validates the
physical Obsidian app and selected Vault/project boundary. When OpenClaw is available, it also
inspects its separate `obsidian-memory-plugin` configuration for the `health-manager` connection.
The preferred OpenClaw multi-agent configuration is:

```json
{
  "plugins": {
    "entries": {
      "obsidian-memory-plugin": {
        "enabled": true,
        "config": {
          "agentConfigs": {
            "health-manager": {
              "agentId": "health-manager",
              "vaultPath": "/absolute/path/to/My Vault",
              "projectId": "cyber-health-agent",
              "projectRoot": "/absolute/path/to/Cyber Health Agent"
            }
          }
        }
      }
    }
  }
}
```

For OpenClaw, the preflight must verify that the plugin is enabled and loaded; `vaultPath` is an absolute,
readable Vault without symlinks; `Vault/20-Projects/<projectId>` exists inside that Vault and is
readable without symlinks; and `Vault/00-System/projects.yaml` declares the same `projectId`.
Missing or invalid configuration must produce an explicit warning identifying the failed boundary
(for example, plugin missing/disabled, no `health-manager` config, unreadable Vault, or missing
project declaration). It must never be reported as a connected Provider. Core SQLite facts can
continue with long-term memory deferred, in which case the normal outbox and `MEMORY_DEFERRED`
warning remain authoritative.

When every check passes, the `cyber-health` MCP registration must pass the validated connection to
Cyber Health:

```text
--memory-provider obsidian
--memory-vault /absolute/path/to/My Vault
--memory-project-id cyber-health-agent
```

Hermes does not use that manifest. Cyber Health registers only its MCP server; install the public
plugin through its [native Hermes instructions](https://github.com/annual30k/obsidian-memory-plugin#hermes).
Cyber Health neither extracts a Skill into `$HERMES_HOME` nor writes Hermes memory environment files.

Without an explicit `--memory-vault`, this only wires an already validated OpenClaw `health-manager`
project and does not modify a plugin or Vault. The plugin remains hook-only, while
`ObsidianMemoryProvider` is the Cyber Health adapter that owns the filesystem boundary and
preserves the Core outbox fallback. See the full
[OpenClaw adapter specification](docs/openclaw-adapter-spec.md) for the install/update checklist.

### One-Vault first-run bootstrap

For the complete user-facing decision flow, use the [Agent onboarding guide](docs/agent-onboarding.md).
This section is the CLI contract, not a request for users to manually configure every dependency.

For a new user who has explicitly agreed to long-term memory, one explicit Vault path authorizes the complete Cyber Health memory setup:

```bash
cyber-health install --memory-vault "/absolute/path/to/My Vault"
```

The installer first verifies that Obsidian is installed. If it is missing, it stops before
changing the Vault or shared plugin and reports `install-required`, so the calling Agent can ask
the user to install Obsidian and open/create the selected Vault. It then previews the remaining
work with `--dry-run`, and creates only missing files inside that selected Vault: the
`Cyber-Health-Agent-<stable-id>` project unit under `20-Projects/`, required project directories
and starter notes, plus an append-only `projects.yaml` registration. For OpenClaw it resolves the
latest stable GitHub Release of `obsidian-memory-plugin`, requires a published SHA-256 digest,
caches the verified tgz under `~/.cyber-health/plugins/`, and merges only the `health-manager`
agent binding and required hook permissions. It never downloads a branch or silently updates on
startup; `cyber-health update` checks the latest Release again. OpenClaw is optional for Vault
project initialization; in a Hermes-only or Codex-only setup, the MCP can still use the validated
Cyber Health project, while the public plugin follows its own host-native installation guide.
Existing notes, other project mappings, and other agents' memory
configurations remain untouched. An existing different
`health-manager` binding, malformed registry, symlinked path, or concurrent configuration change
fails closed rather than being overwritten. The command never guesses a Vault; supplying
`--memory-vault` is the required authorization.

If the preflight reports `unconfigured` or `invalid`, the Agent must give the user the
next configuration action (install/enable `obsidian-memory-plugin`, configure the
`health-manager` agent's `vaultPath` and `projectId`, or repair the Vault project
layout) and must not silently proceed as if long-term memory were available. The
installer does not modify another user's plugin configuration or Vault without explicit long-term-memory authorization.

### CLI Arguments & Environment Variables

#### Cyber Health MCP Server (`cyber-health-mcp`)
| Parameter / Flag | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--db <path>` | `CYBER_HEALTH_DB` | `./data/cyber-health.sqlite3` | Path to SQLite database file |
| `--allow-all` | `CYBER_HEALTH_ALLOW_ALL_TOOLS` | `false` | When true, exposes all 22 extended domain tools (29 tools total); defaults to 7 P0 tools |

#### Cyber Health Uninstaller (`cyber-health-uninstall`)
| Parameter / Flag | Default | Description |
| :--- | :--- | :--- |
| `--dry-run` | `false` | Deterministic inspection and reporting without modifying any host or file state |
| `--json` | `false` | Output structured machine-readable report in JSON format |
| `--db <path>` | `./data/cyber-health.sqlite3` | Path to SQLite database file to verify / protect |
| `--purge-data` | `false` | Opt-in flag to delete data files (requires `--confirm-purge`) |
| `--confirm-purge <token>` | `None` | Mandatory verification token (`DELETE_CYBER_HEALTH_DATA`) when `--purge-data` is specified |
| `--force-foreign-host-mcp` | `false` | Recovery-only override to unset OpenClaw MCP server if ownership paths are ambiguous (strictly requires `--confirm-foreign-unset`) |
| `--confirm-foreign-unset <token>` | `None` | Mandatory recovery token (`UNSET_FOREIGN_CYBER_HEALTH`) required when `--force-foreign-host-mcp` is set |

---

## Tool Surface & Implementation Status

Hosts load the tool list into every session, so the server publishes a compact listing: generated schema titles and redundant defaults are removed, every tool declares its response envelope as an `outputSchema`, and safety annotations stay explicit. Detailed workflows are available on demand through the `nightly_review` MCP prompt and the read-only `cyber-health://profile` and `cyber-health://today` resources.

### P0 Core Tools (Default Allowlist - 7 Tools)

| Tool Name | Type | Status | Description |
| :--- | :--- | :--- | :--- |
| `cyber_health_get_profile` | Read | **Completed** | Read-only profile query without DB mutation |
| `cyber_health_update_profile` | Write | **Completed** | First-run intake and later profile/goal updates |
| `cyber_health_get_today` | Read | **Completed** | Snapshot query for date facts, maintenance recommendation, and generation keys |
| `cyber_health_log_meal` | Write | **Completed** | Meal logging with revision chain, repeat meal, and idempotency |
| `cyber_health_get_audit_trail` | Read | **Completed** | Chronological operation logs with before/after versions |
| `cyber_health_health_check` | Read | **Completed** | SQLite status, MemoryProvider state, pending outbox work |
| `cyber_health_get_schedule` | Read | **Completed** | Pure derived read returning dynamic eligibility, suppression reasons, and tombstones |

### Extended Domain Tools (Enabled with `--allow-all` - 22 Additional Tools, 29 Total)

| Tool Name | Status | Description |
| :--- | :--- | :--- |
| `cyber_health_log_daily_metrics` | **Completed** | Daily metric submissions, merged state, TRAIN_RECOVERY_01 fatigue evaluation |
| `cyber_health_delete_meal` | **Completed** | Soft deletion of active meal and balance recalculation |
| `cyber_health_log_workout` | **Completed** | Workout log, wearable/screenshot facts and original-image retention, red-flag detection, restricted mode block |
| `cyber_health_daily_review` | **Completed** | Nightly audit, unrecorded handling, revision-linked draft plan |
| `cyber_health_plan_tomorrow` | **Completed** | Centralized safety, draft/committed state transitions |
| `cyber_health_acknowledge_schedule_event` | **Completed** | Acknowledging, delivering, skipping, or postponing schedule reminders |
| `cyber_health_maintain_memory` | **Completed** | Out-of-lock outbox drainage, generation key continuation, owner token lease, and lineage-preserving TTL |
| `cyber_health_get_remaining_calories` | **Completed** | Remaining calorie/protein budget with macro-prioritized next meal recommendation |
| `cyber_health_get_training_plan` | **Completed** | Evidence-based exercise prescription generator respecting safety rules and recovery state |
| `cyber_health_complete_workout` | **Completed** | Minimal workout check-in with red-flag detection and progression state update |
| `cyber_health_confirm_training_progression` | **Completed** | Transaction-isolated progression confirmation with signature digest and re-checked safety gates |
| `cyber_health_substitute_exercise` | **Completed** | Real-time exercise substitution preserving movement pattern and weekly volume |
| `cyber_health_query_knowledge` | **Completed** | Peer-reviewed sports nutrition and cardiovascular exercise safety guidelines lookup |
| `cyber_health_export_data` | **Completed** | Portable SQLite snapshot export with full audit trails and revision chains |
| `cyber_health_import_data` | **Completed** | Portable health facts snapshot import with strict schema validation and atomic rollback |
| `cyber_health_memory_action` | **Completed** | Pre-validated memory candidate action with destructive annotations |
| `cyber_health_schedule_daily_reminders` | **Completed** | Deterministic 5-window reminder generator with postponement protection |
| `cyber_health_update_schedule_event` | **Completed** | Scheduled event status, delivery, or postponed time window updates |
| `cyber_health_query_memory` | **Completed** | Dual-layer memory query retrieving short-term SQLite facts and long-term Obsidian memories |
| `cyber_health_get_memory_suggestions` | **Completed** | Read-only multi-day pattern detection that returns user-confirmation candidates without writing memory |
| `cyber_health_get_weight_trend` | **Completed** | Read-only 7-day average and weekly weight change judged against the goal type (fat loss −0.5–1%/week, muscle gain +0.25–0.5%/week, maintain ±0.25%); calorie adjustments are suggestions that need user consent |
| `cyber_health_weekly_review` | **Completed** | Read-only weekly review: logging coverage, intake vs target, protein days, workouts, sleep, weight trend and data gaps; unrecorded days are never counted as zero |

---

## Host Integration & Uninstallation Engine (`cyber-health-uninstall`)

The packaged console entry point `cyber-health-uninstall` cleanly manages host registrations and provides safe uninstallation:

- **Production-Safe Data Preservation**: By default, `cyber-health-uninstall` **strictly preserves** all SQLite databases (`*.sqlite3`, `*-wal`, `*-shm`), exports, `.venv`, distribution wheels, and user data.
- **Strict Cross-Project Non-Interference**: `obsidian-memory` is a separate cross-project plugin and **not** a Cyber Health component. The uninstaller **never** uninstalls, disables, edits, or deletes `obsidian-memory`, its Codex plugin/marketplace/cache, any Obsidian Vault, or unrelated OpenClaw configurations.
- **Codex Entry Isolation**: Codex integration manages only the fixed `cyber-health` MCP entry through `codex mcp get/add/remove`; unrelated Codex configuration is preserved. Cyber Health itself remains an independent Python Core + MCP server, not an Obsidian plugin.
- **Hermes Entry Isolation**: Hermes integration manages only the fixed `cyber-health` MCP entry through `hermes mcp add/test/remove`; it verifies the persisted command, arguments, ownership fingerprint, enabled state, and required tool discovery while preserving unrelated Hermes configuration.
- **Strict Ownership Verification**: OpenClaw MCP entries (`cyber-health`) are inspected via `openclaw mcp show cyber-health --json`. Removal via `openclaw mcp unset` (never `remove`) is performed only when command, cwd, or database parameters demonstrably point to this repository root. Foreign or ambiguous entries are hard-refused unless the recovery-only override `--force-foreign-host-mcp` is explicitly paired with `--confirm-foreign-unset UNSET_FOREIGN_CYBER_HEALTH`.
- **Safe LaunchAgent Management**: LaunchAgents are handled only for the fixed project label (`ai.cyber-health.agent`) at `~/Library/LaunchAgents/ai.cyber-health.agent.plist`, and ownership is proven from program/working directory paths. Arbitrary label targeting and pattern deletion are prohibited.
- **Gated Data Purge (`--purge-data`)**: Opt-in data removal strictly requires the strong confirmation token `--confirm-purge DELETE_CYBER_HEALTH_DATA`. Symlinks, path traversals (`..`), root/home/broad system directories, and out-of-boundary paths are rejected. Approved targets prefer recoverable trash semantics and never inspect or display health contents.
- **Idempotency & Clean No-Ops**: Unregistered integrations or repeated executions succeed cleanly as no-ops.

---

## Automated Test Suite

Run the linter and the full test suite (CI runs both):

```bash
uvx ruff@0.16.10 check .
.venv/bin/python -m unittest discover -s tests -v
```

CI also runs `scripts/upgrade_smoke.py` on Linux, macOS and Windows: the previous published Release installs itself, then upgrades to the current build with its own updater (version hand-off included, new build installed from a wheel). Only the GitHub "latest Release" lookup is pointed at the local wheel; the Release workflow will not publish unless it passes. Run it locally with `uv run python scripts/upgrade_smoke.py` (add `--parent-wheel PATH` to choose the starting version).

Date-sensitive tests use the injectable service clock (`CyberHealthService(..., clock=...)`, see `tests/test_support.py`), and installer/updater/uninstaller tests never discover the developer's real Codex or Hermes CLIs.

The suite contains **283 automated test cases**, organized by feature:

### Domain behavior
- `tests/test_profile_and_onboarding.py`: grouped first-run intake, automation declaration and plan gating.
- `tests/test_meals_and_daily_totals.py`: meal validation, revisions, repeat/delete, read-only snapshots, timezone-aware day grouping and intake uncertainty aggregation.
- `tests/test_idempotency.py`: required keys, exact replay vs `IDEMPOTENCY_MISMATCH`, key reuse after the retry window, stale-review recomputation and `CONFLICT_VERSION`.
- `tests/test_safety_and_recovery.py`: red flags, restricted mode, deload protocol, sleep/fatigue recovery rules and evidence freshness.
- `tests/test_workout_logging.py`: wearable screenshot facts and originals across sessions, workout completion.
- `tests/test_training_plan.py`: prescription decision matrix, combined constraints, structured rest and exercise substitution.
- `tests/test_training_progression.py`: double progression, streak breakers, confirmation evidence and safety/recovery blocks.
- `tests/test_schedule.py`: five reminder windows, eligibility suppression, postponement, tombstones, overdue compensation and read purity.
- `tests/test_daily_review.py`: nightly fact collection, daily review, tomorrow's plan and maintenance hints.
- `tests/test_progress.py`: weight trend classification against the goal, suggestion-only calorie adjustments, weekly review coverage and read purity.
- `tests/test_import_export.py`: export/import round trip, schema and value validation, safety-profile protection and legacy-owner import.
- `tests/test_knowledge.py`: evidence citations and non-diagnostic disclosures.

### Long-term memory
- `tests/test_memory_proposals_and_outbox.py`: proposals and memory actions, pre-reserved intents, leases, batch continuation, provider failure backoff and TTL pruning.
- `tests/test_memory_trends_and_recall.py`: weekly trend consolidation, missing-day disclosure, revision chains and dual-layer recall.
- `tests/test_memory_suggestions.py`: read-only active-memory pattern suggestions.
- `tests/test_obsidian_memory_provider.py`: Obsidian provider bridge, inspector scope checks and note parsing.

### MCP server, storage and host integration
- `tests/test_mcp_server.py`: stdio tool discovery (7 P0 vs 29 total), single-owner boundary, console entrypoint and error envelopes.
- `tests/test_store_migrations.py` and `tests/test_migrate_owner.py`: versioned schema migrations and legacy-partition migration.
- `tests/test_codex_registration.py` and `tests/test_hermes_registration.py`: isolated native MCP registration lifecycles, with optional real-CLI round trips.
- `tests/test_installer.py`, `tests/test_updater.py`, `tests/test_uninstaller.py`, `tests/test_cli.py`: install/update/uninstall contracts, fail-closed ordering, purge safety and TOCTOU defenses.
- `tests/test_core_release.py`, `tests/test_memory_plugin_release.py`, `tests/test_memory_bootstrap.py`: verified Release resolution and one-Vault memory bootstrap.

---

## Truth-in-Advertising & External Boundaries

> [!IMPORTANT]
> **Declaration of System Status & Physical Boundaries**:
> The local Cyber Health Core engine, stdio MCP server, and host integration lifecycle have completed automated verification within the v0.6.4 scope. However, **this does not constitute production deployment or physical external integration**:
> 1. **Obsidian Vault / MemoryProvider: Conditional connection only**: If the install/update preflight is incomplete, or the Provider is unavailable, Cyber Health does not connect to the Vault and keeps long-term memory in deferred/outbox processing (`MEMORY_DEFERRED`). When the preflight passes and the `cyber-health` MCP registration includes the validated `--memory-provider obsidian`, `--memory-vault`, and `--memory-project-id` parameters, Cyber Health can connect to the verified `health-manager` project. No connection is implicit, and explicit provider authorization remains required.
> 2. **Cross-Project Plugin Boundaries**: `obsidian-memory` is a separate cross-project plugin and is never modified, disabled, or removed by Cyber Health Agent tools.
> 3. **Host Active Push Notifications: Not Registered**: Core is a headless request-response MCP server that outputs dynamic trigger conditions, suppression reasons, and tombstones. Active push notifications require a host-level scheduler or daemon (e.g. OpenClaw Cron, Launchd).
> 4. **Clinical Physician Review: Pending**: Built-in evidence guidelines carry mandatory `NON_DIAGNOSTIC` legal disclaimers. Acute red-flag symptoms immediately block workouts and require emergency offline consultation.
> 5. **Multimodal Vision & Wearables: Handled by Host**: Food photo analysis and native Apple Health/Garmin Bluetooth syncing are host-level capabilities; Core processes structured numerical facts.
