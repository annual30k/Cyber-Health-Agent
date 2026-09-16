# Cyber Health Agent (Core & stdio MCP v0.3.5)

> Independent, pluggable deterministic health engine and stdio MCP server for AI hosts (Codex, OpenClaw, Hermes, etc.). It is not an Obsidian plugin.
> **Current Status**: Core P0 implementation and extended domain capabilities (27 tools total: 7 P0 + 20 extended), including first-run intake, nightly fact collection, target-gap analysis, host automation declarations, next-day plan generation, cross-session wearable screenshot retention, and read-only active-memory pattern suggestions.

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
- **Strict Read-Only Purity & Consistent Snapshots**: `cyber_health_get_profile` and `cyber_health_get_today` are strictly pure snapshot queries and never insert or mutate database records. `get_today` uses explicit snapshot read transactions (`BEGIN` ... `COMMIT`).
- **Idempotency & Concurrency**: All state-mutating operations strictly require a non-empty `idempotency_key`. Replays return cached responses; payload mismatches raise `IDEMPOTENCY_MISMATCH`. Stale writes raise `CONFLICT_VERSION`.
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
- **Fact Migration & Integrity**: Full safety profiles, revision chains, and schedules are exported and restored idempotently. Duplicate IDs with conflicting data raise `ConflictError` rather than being silently ignored.
- **Evidence-Based Knowledge Retrieval**: Only verified primary literature citations (ISSN, AHA) with valid DOIs/URLs are returned; queries without verified matches return `unavailable` without returning irrelevant items.

---

## Standard Installation Contract (for Agents and Users)

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

# 3. Run extended server exposing all 27 verified domain tools
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
| `--allow-all` | `CYBER_HEALTH_ALLOW_ALL_TOOLS` | `false` | When true, exposes all 20 extended domain tools (27 tools total); defaults to 7 P0 tools |

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

### Extended Domain Tools (Enabled with `--allow-all` - 20 Additional Tools, 27 Total)

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

Run the full test suite using `unittest`:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Current test suite contains **245 automated test cases** across 32 test files (100% passing), including Codex and Hermes host registration, Hermes public-memory Skill adaptation, host-neutral one-Vault memory bootstrap, Obsidian-install preflight, active-memory suggestion, provider bridge, installer, updater, and package-version consistency coverage:

### Part A. Codex Review & Independent Verification Suites (89 tests)
- `tests/test_codex_review.py` (8 tests): Round 1 regressions (mandatory idempotency keys, calendar validation, range checks, repeat resolution).
- `tests/test_codex_review_round2.py` (7 tests): Round 2 regressions (console entrypoint, outbox isolation, pre-commit intent, red-flag mode, DST offsets).
- `tests/test_codex_outbox_concurrency.py` (3 tests): Round 3 concurrency state machine (delimiter collisions, pre-reservation, task stealing prevention).
- `tests/test_codex_review_round4.py` (4 tests): Round 4 regressions (profile safety restoration, full payload hashing, non-stealing lease re-entrancy, unified sleep < 6h).
- `tests/test_codex_import_safety.py` (2 tests): Round 5 regressions (safety mode protection on import, normalized field conflict detection).
- `tests/test_codex_import_validation.py` (4 tests): Round 6 regressions (unsupported schema rejection, malformed JSON rollback, invalid timezone/mode).
- `tests/test_codex_memory_evidence.py` (2 tests): Round 7 regressions (memory status/confidence fidelity, strict limit truncation).
- `tests/test_codex_unconfigured_plan.py` (2 tests): Round 8 regressions (unconfigured target disclosure, zero default calories rejection).
- `tests/test_codex_review_round9.py` (9 tests): Round 9 regressions (double progression state machine, combined constraints, stale evidence expiration, movement substitution).
- `tests/test_codex_review_round10.py` (14 tests): Round 10 regressions (failed session break streak, same-day consolidation, proposal signature verification, baseline load separation).
- `tests/test_codex_progression_fatigue.py` (8 tests): Round 11 regressions (shared pure safety evaluation, nested metrics extraction, zero-value fidelity, future date filtering).
- `tests/test_codex_schedule_sync.py` (8 tests): Round 12 regressions (5 standard reminder windows, postponement preservation, tombstone snapshots, pure-read schedule).
- `tests/test_codex_review_round13.py` (10 tests): Round 13 regressions (partial workout non-suppression, missing completion rate non-suppression, superseded plan filtering, pure-read snapshot isolation, scoped maintenance host drain).
- `tests/test_codex_review_round14.py` (8 tests): Round 14 regressions (TTL meal detail detection, expired in-flight worker recovery, 51+ task continuation without idempotency block, provider failure backoff, review/rule decoupling, stdio MCP continuation).

### Part B. Gemini Domain Contract & System Regression Suites (41 tests)
- `tests/test_p0_contracts.py` (6 tests): P0 contracts (read-only purity, 5-session flow, idempotency hash match vs mismatch, version conflict rejection, timezone-aware day grouping, input validation).
- `tests/test_domain_advanced.py` (6 tests): Advanced domain logic (meal deletion & repeat, recovery score, red flag lock & deload protocol, review/plan transitions, schedule lifecycle, memory outbox queueing & retry).
- `tests/test_cross_session.py` (4 tests): Cross-session persistence and optimistic concurrency.
- `tests/test_mcp_stdio.py` (2 tests): Cross-process stdio MCP client tests verifying tool discovery (7 P0 vs 27 total) and stdio execution without warnings.
- `tests/test_outbox_concurrency_extended.py` (4 tests): Outbox extensions (concurrent replay, batch chunking at 50, crashed worker lease recovery, physical TTL pruning).
- `tests/test_domain_remaining.py` (6 tests): Training plan states, workout check-in red flags, knowledge query disclosures, data export/import round-trip, schedule event lifecycle, and MCP error envelope input sanitization.
- `tests/test_domain_memory_and_trends.py` (13 tests): Dual-layer memory query, remaining calorie guidance, weekly trend aggregation, missing day disclosure, idempotent maintenance, late meal revision chains, detail pruning, and exercise decision matrix.

### Part C. Isolated Host & Uninstallation Safety Suites
- `tests/test_codex_integration.py` (4 tests): isolated Codex add/get/remove lifecycle, foreign same-name refusal, unrelated-config preservation, and a real CLI round trip under an isolated `CODEX_HOME`.
- `tests/test_hermes_integration.py` (6 tests): isolated Hermes native add/test/remove lifecycle, disabled-entry repair, foreign same-name refusal, malformed-YAML fail-closed behavior, unrelated-config preservation, and a real CLI round trip under an isolated `HERMES_HOME`.
- `tests/test_memory_plugin_release.py` (3 tests): stable public Release resolution, mandatory SHA-256 verification, atomic caching, and tamper refusal.
- `tests/test_core_release.py` (3 tests): Core wheel Release resolution, mandatory SHA-256 verification, cache reuse, and tag/version mismatch refusal.
- `tests/test_memory_bootstrap.py` (6 tests): explicit Vault bootstrap, Obsidian-install preflight,
  plugin installation, append-only project registration, other-agent preservation, repeat-run
  idempotency, different-binding refusal, and dry-run purity.
- `tests/test_uninstaller.py` (30 tests): Host integration uninstallation contracts (including packaged-CLI OpenClaw auto-detection, exact-state fingerprints and global preflight, fail-closed execution ordering, deterministic dry-run purity, normal data preservation, sanitized reporting without raw environment leakage, refusal of unrelated/foreign OpenClaw registrations, project-root prefix collision rejection, command signature spoofing rejection, double confirmation token for foreign unsets, CLI inspection error fail-closed handling with secret redaction, explicit `--confirm-purge` token requirement, TOCTOU post-plan symlink/inode/host-state swap defenses, refusal of destructive purge when host inspector is missing, fixed LaunchAgent label enforcement, project-local `.trash` symlink rejection, preservation of unknown files in data directory, idempotent repeat execution, non-interference with `obsidian-memory`, Obsidian Vaults, and Codex state, and isolated live OpenClaw sandbox probe).
- `tests/test_onboarding_flow.py` (5 tests): First-run grouped intake, automation declaration, nightly missing-fact questions, target-gap/workout analysis, detailed tomorrow plan, read purity, and plan gating.

---

## Truth-in-Advertising & External Boundaries

> [!IMPORTANT]
> **Declaration of System Status & Physical Boundaries**:
> The local Cyber Health Core engine, stdio MCP server, and host integration lifecycle have completed automated verification within the v0.3.5 scope. However, **this does not constitute production deployment or physical external integration**:
> 1. **Obsidian Vault / MemoryProvider: Conditional connection only**: If the install/update preflight is incomplete, or the Provider is unavailable, Cyber Health does not connect to the Vault and keeps long-term memory in deferred/outbox processing (`MEMORY_DEFERRED`). When the preflight passes and the `cyber-health` MCP registration includes the validated `--memory-provider obsidian`, `--memory-vault`, and `--memory-project-id` parameters, Cyber Health can connect to the verified `health-manager` project. No connection is implicit, and explicit provider authorization remains required.
> 2. **Cross-Project Plugin Boundaries**: `obsidian-memory` is a separate cross-project plugin and is never modified, disabled, or removed by Cyber Health Agent tools.
> 3. **Host Active Push Notifications: Not Registered**: Core is a headless request-response MCP server that outputs dynamic trigger conditions, suppression reasons, and tombstones. Active push notifications require a host-level scheduler or daemon (e.g. OpenClaw Cron, Launchd).
> 4. **Clinical Physician Review: Pending**: Built-in evidence guidelines carry mandatory `NON_DIAGNOSTIC` legal disclaimers. Acute red-flag symptoms immediately block workouts and require emergency offline consultation.
> 5. **Multimodal Vision & Wearables: Handled by Host**: Food photo analysis and native Apple Health/Garmin Bluetooth syncing are host-level capabilities; Core processes structured numerical facts.
