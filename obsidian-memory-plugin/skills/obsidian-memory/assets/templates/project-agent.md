---
type: project-schema
project_id: "{{project_id}}"
project_root: "{{project_root}}"
schema_state: pending-first-ingest
---

# {{project_name}} memory contract

Inherit the Vault root AGENTS.md. Keep this project's evidence private. These
local rules cannot weaken the root capture, ingest, privacy or source boundaries.
Read rules.md for user-supplied conventions; do not infer missing business facts.

## Schema state

The first meaningful user-authorized ingest establishes the taxonomy from its
selected sources. Update schema_state to established only when the category
table and actual derived pages have been verified.

## Starter categories

| Path | Purpose | Page type |
| --- | --- | --- |
| wiki/decisions/ | Durable evidence-backed choices | decision |
| wiki/pitfalls/ | Verified causes and prevention | pitfall |
| wiki/knowledge/ | Reusable synthesis | knowledge |
| checkpoints/ | Meaningful unfinished work | checkpoint |

Retain useful starter categories; add or refine categories only when supported
by actual sources. This table describes storage roles, not project facts.

## Common frontmatter

Derived notes retain title, type, scope, project_id, status, created, last_updated,
sensitivity and sources. Sources are quoted full-Vault-relative Raw wikilinks.
Inbox and Raw use the stable identity and evidence fields from their templates.

## Workflow and navigation

Recall from index.md, Wiki, Raw and Checkpoints; Inbox is pending evidence.
Qualified capture goes to Inbox. Only a user-triggered ingest freezes Raw,
updates canonical knowledge and index/log, then marks verified candidates ingested.
Keep Raw immutable and log append-only. Serialize writes in this project.

[[20-Projects/{{project_id}}/index|Index]] · [[20-Projects/{{project_id}}/log|Log]]
