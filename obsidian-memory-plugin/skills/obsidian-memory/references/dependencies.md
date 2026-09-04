# Install and diagnose the complete host skill suite

Read when a required Obsidian skill is unavailable, during connection setup, or
when a different version appears to run. Do not install anything just because a
skill name is missing from the current model's catalog.

## Full installation, selective loading

The user chose the entire [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills)
collection as a host dependency. The upstream catalog verified on 2026-09-04 is:

| Skill | Upstream directory | Load when |
| --- | --- | --- |
| obsidian-cli | skills/obsidian-cli | A requested operation needs the running Obsidian app or its CLI-only capabilities |
| obsidian-markdown | skills/obsidian-markdown | Creating or editing Obsidian Markdown |
| obsidian-bases | skills/obsidian-bases | Working with .base files |
| json-canvas | skills/json-canvas | Working with .canvas files |
| defuddle | skills/defuddle | Extracting clean content from webpages |

This is an installation baseline, not an instruction to load every SKILL.md into
every task or to generate unrequested Bases/Canvas artifacts. Check the complete
`skills/*/SKILL.md` catalog at the selected upstream revision during setup/update;
do not permanently limit installation to this five-name snapshot. Record the
selected revision and per-skill installed/visible/eligible status in the setup
receipt. If upstream cannot be checked, state that completeness is unverified.
Do not install unrelated repositories or all host-global skills.

## Diagnose and complete setup

1. Inspect the host's supported skill inventory/info and current Agent's visible
   skills for every suite member, not only CLI and Markdown. Inspect only relevant
   locations reported by the host; do not search the entire home directory.
2. Classify the result:

   | State | Response |
   | --- | --- |
   | Installed and visible/eligible | Reuse it; read the actual selected SKILL.md only when relevant to the task. |
   | Installed but hidden/disabled | Report the Agent's visibility, allowlist or enablement issue; do not install a second copy. |
   | Installed but ineligible | Inspect the reported missing prerequisite, such as the CLI executable; another skill install will not fix it. |
   | Multiple copies or unexpected source | Inspect precedence and the selected path. Do not delete or overwrite other copies. |
   | Confirmed absent | Include it in the full suite's missing set; install through the host's documented installer when authorized. |
   | Inventory unavailable/ambiguous | State that installation status is unverified; request the minimum information needed. Do not treat uncertainty as absence. |

3. Permission to use memory does not by itself authorize installing software or
   changing an Agent's allowlist/global configuration. If the user has already
   authorized the specific installation/configuration change, perform it within
   that scope; otherwise explain the needed change and obtain authorization.
4. For the confirmed missing set, use
   [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) and the
   current host's supported installer. Choose the complete upstream collection,
   preserving already-installed copies and adding every missing member at the
   selected revision. If only CLI and Markdown exist, add Bases, JSON Canvas and
   Defuddle rather than reporting setup complete. Do not invent a CLI command,
   assume a universal install directory, or implement a substitute.
5. Re-check every member's selected source, visibility and eligibility after changes. Follow
   the host's reload/new-session requirements. Do not claim a skill is active
   merely because files were downloaded; if not yet visible, report what remains.
   Skills and executable prerequisites are separate: for example, a Defuddle
   skill file does not prove its CLI is installed. Follow that skill's documented
   prerequisites and the user's installation scope, never silently install extra
   software or claim an unavailable capability works.
6. Load the task-relevant external skill, then verify the exact physical Vault
   path for filesystem memory operations. Verify the CLI only when the requested
   operation needs Obsidian application capabilities. A visible skill does not
   prove that Obsidian is connected.

For OpenClaw, plugin skills can be shadowed by higher-priority same-name skills.
Check the actual source of `obsidian-memory` too, not just the external skills.
The plugin hook points to its bundled entry; when testing an explicit Skill
invocation, verify it is not invoking an older host-installed copy instead.
Use the installed host's help/info and
[official Skills documentation](https://docs.openclaw.ai/tools/skills) for current
visibility and precedence behavior, not guessed commands.

If a member remains unavailable, report full-suite setup as incomplete and pause
operations needing it. Other verified capabilities may still be used when safe;
for example, a missing CLI does not block a selected Vault's managed Markdown
memory operations, while a missing Defuddle must not be concealed. If a requested
write cannot run, state that nothing was saved. Filesystem memory access is an
explicit SKILL.md transport, not an undisclosed fallback.
