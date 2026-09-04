# Vault Layout Contract

Adapted from the supplied Obsidian Memory Skill; no new storage engine or schema
migration is required.

```text
AGENTS.md
Home.md
00-System/
  projects.yaml
  templates/
10-Global/
  preferences.md
  inbox/assets/
  raw/assets/
  index.md
  log.md
20-Projects/<project_id>/
  AGENTS.md
  rules.md
  inbox/assets/
  raw/assets/
  wiki/decisions/
  wiki/pitfalls/
  wiki/knowledge/
  checkpoints/
  index.md
  log.md
30-Shared/
```

`projects.yaml` has a `projects` list. Existing IDs must remain stable:

```yaml
projects:
  - id: example-project
    roots:
      - /absolute/path/to/example-project
    scope: private
```

Use the longest physical-directory match. Never infer a code root from an
Agent's generic workspace or Gateway cwd. A non-code topic may be created with
an explicit user-approved ID and `roots: []`; it must be selected explicitly.

Inbox is editable pending evidence. User-triggered ingest freezes the selected
evidence into Raw before updating derived Wiki/Checkpoint/preference notes.
Retained attachments go through inbox/assets then exact-copy raw/assets.

At first meaningful ingest, establish the project taxonomy in its AGENTS from
selected evidence, rather than assuming every project needs the same categories.

Derived notes record at least `title`, `type`, `scope`, `project_id`, `status`,
`created`, `last_updated`, `sensitivity` and `sources`. Sources are quoted
full-Vault-relative Wikilinks to Raw. Use the
host's obsidian-markdown skill for formatting; this file does not duplicate it.

Raw immutability is a workflow rule, not a physical filesystem guarantee. Do not
rename or rewrite Raw; report observed external corruption without guessing a
replacement. Index is navigation; log is append-only.

Before creating memory notes, use [templates.md](templates.md). Fixed evidence
fields keep notes interoperable; the project's business taxonomy can evolve.
Before capture or ingest, use [identity-and-recovery.md](identity-and-recovery.md)
for stable candidate/Raw paths and retry behavior. Existing IDs, Raw paths and
user templates are preserved; plugin upgrades do not authorize a Vault migration.
