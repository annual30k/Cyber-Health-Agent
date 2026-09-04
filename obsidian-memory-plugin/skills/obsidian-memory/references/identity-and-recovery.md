# Candidate identity and interrupted-ingest recovery

These are serial workflow conventions implemented by the current Agent through
the host's Obsidian skills, not locks, transactions or a second storage engine.

## Stable identity

For a new candidate, generate one UUID using an available host facility and set
`id: cand-<lowercase-uuid>`. Validate it as a safe filename component. The ID is
per candidate, not per conversation, title, ingest attempt or model response.
Keep it unchanged after creation, including retries and title edits.

Within the already selected scope (`10-Global` or `20-Projects/<project_id>`):

| Artifact | Identity/path |
| --- | --- |
| Inbox | `inbox/<candidate_id>.md` |
| Raw | `raw/raw-<candidate_id>.md`; `id: raw-<candidate_id>`; `candidate_id: <candidate_id>` |
| Candidate's final Raw reference | `raw_record: "[[<scope>/raw/raw-<candidate_id>]]"` |
| Pending attachment | `inbox/assets/<candidate_id>/<original-name>` |
| Frozen attachment | `raw/assets/<candidate_id>/<original-name>` |
| Append-only ingest log marker | `ingest_id: ingest-<candidate_id>` |

The marker is a plain line beneath the normal dated ingest heading. Write one
completed ingest entry per candidate, including within a batch. Quote YAML
wikilinks and use full Vault-relative paths. Do not derive paths from raw titles,
source URLs or attachment-provided paths. Reject escaping paths. If two different
attachments have the same basename, allocate distinct stable filenames before
creating either and preserve that mapping in the candidate.

Before capture, compare the relevant source reference and evidence against
existing pending candidates and canonical knowledge in the selected scope. An
exact repeat reuses the existing item; changed evidence or a new source may
justify a new candidate. Do not deduplicate solely by title. If a capture response
was interrupted, inspect the candidate path and read it back before retrying.

## Ingest and retry

1. Read the candidate, its ID/status/Raw reference, deterministic Raw path,
   relevant canonical pages, index and the stable log marker. If the candidate
   is already ingested and the outputs are complete, return an already-ingested
   receipt without writing. Do not treat `status: ingested` alone as proof.
2. For old candidates with no ID or an existing nonstandard Raw link, preserve
   their paths and links. Resolve existing evidence first. Add an ID to a pending
   candidate only after checking for an earlier ingest; reuse a verified existing
   Raw instead of renaming or duplicating it. Ambiguous identity requires a user
   decision, not automatic migration. Do not add fields to immutable legacy Raw.
3. If Raw does not exist, re-read the candidate and freeze its factual evidence,
   original source reference, capture context, sensitivity and attachment bytes.
   `created` on Raw is the freeze time; `captured_at` is the candidate's creation
   time. Keep conclusions out of Raw. Retain the Inbox candidate and its assets.
4. If Raw exists, verify its candidate identity, scope, original evidence/source
   and all retained attachments against the candidate snapshot. Reuse it only
   on a match. A partial Raw, conflicting ID, changed evidence or mismatched
   attachment is a blocker: report it, preserve both, and ask how to resolve it.
   Do not overwrite Raw or invent a new ID to evade the conflict.
5. On retry after Raw creation, update only missing synthesis, backlinks, index
   entries and log work. Search canonical pages and check Raw links before adding
   paragraphs. Avoid duplicate source links and index entries. If the ingest log
   marker already exists, do not append a second completed-ingest entry.
6. Verify the actual derived/index/log outputs and evidence links, then set the
   candidate to `status: ingested` and its `raw_record` link. Until verified it
   remains pending. A duplicate source need not create a new Wiki page, but its
   disposition and canonical destination must be clear in the ingest log.
7. On failure, report the candidate ID, completed paths and remaining steps. Do
   not claim the batch succeeded when only some candidates completed. Do not
   automatically roll back successful writes or remove evidence.

Once Raw exists, edits to the source evidence must become a new correction
candidate referencing that Raw; the frozen record stays unchanged. Metadata
updates for completing the candidate are not evidence changes. If a concurrent
writer is detected or a pre-write reread differs, stop writes in that scope and
request/resume a serial run. These checks do not guarantee race-free execution.
