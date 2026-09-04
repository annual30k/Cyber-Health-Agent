import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const templateRoot = new URL("../skills/obsidian-memory/assets/templates/", import.meta.url);
const read = name => readFileSync(new URL(name, templateRoot), "utf8");

// These shipped templates deliberately use only flat, scalar/empty-list YAML.
// This checks their artifact contract, not arbitrary YAML or LLM compliance.
function frontmatter(name) {
  const sections = read(name).split("---\n");
  assert.equal(sections[0], "", name + ": frontmatter must be first");
  assert.ok(sections[1] && sections[2], name + ": frontmatter and body required");
  const fields = {};
  for (const line of sections[1].trim().split("\n")) {
    const separator = line.indexOf(": ");
    assert.ok(separator > 0, name + ": use the flat template subset");
    const key = line.slice(0, separator);
    const value = line.slice(separator + 2);
    assert.ok(!Object.hasOwn(fields, key), name + ": duplicate property " + key);
    // Quoted strings, null, integers and empty arrays are JSON-compatible YAML.
    if (value.startsWith('"') || value === "null" || value === "[]" || /^\d+$/.test(value)) {
      fields[key] = JSON.parse(value);
    } else {
      assert.match(value, /^[a-z][a-z0-9-]*$/, name + ": unsupported scalar");
      fields[key] = value;
    }
  }
  return fields;
}

const templates = readdirSync(templateRoot).sort();

test("the complete bootstrap template set is shipped", () => {
  assert.deepEqual(templates, [
    "checkpoint.md", "credential-reference.md", "decision.md", "global-index.md",
    "global-log.md", "global-preferences.md", "inbox-memory-candidate.md",
    "knowledge.md", "pitfall.md", "project-agent.md", "project-index.md",
    "project-log.md", "project-rules.md", "raw-task-record.md"
  ]);
});

test("all template frontmatter is valid in the supported flat subset", () => {
  for (const name of templates) assert.equal(typeof frontmatter(name).type, "string", name);
});

test("candidate templates preserve pending status and have no premature Raw link", () => {
  for (const name of ["inbox-memory-candidate.md", "credential-reference.md"]) {
    const fields = frontmatter(name);
    assert.equal(fields.schema_version, 1);
    assert.equal(fields.id, "{{candidate_id}}");
    assert.equal(fields.scope, "{{scope}}");
    assert.equal(fields.project_id, null, "Global candidates must not require a project");
    assert.equal(fields.status, "pending-ingest");
    assert.equal(fields.raw_record, null);
    assert.equal(fields.source_ref, "{{source_ref}}");
    assert.equal(fields.created, "{{created}}");
    assert.deepEqual(fields.attachments, []);
  }
  assert.equal(frontmatter("credential-reference.md").sensitivity, "confidential");
});

test("Raw identity is linked to the candidate, not the ingest attempt", () => {
  const fields = frontmatter("raw-task-record.md");
  assert.equal(fields.schema_version, 1);
  assert.equal(fields.id, "raw-{{candidate_id}}");
  assert.equal(fields.candidate_id, "{{candidate_id}}");
  assert.equal(fields.status, "frozen");
  assert.equal(fields.captured_at, "{{captured_at}}");
  assert.equal(fields.created, "{{created}}");
  assert.equal(fields.source_ref, "{{source_ref}}");
  assert.deepEqual(fields.attachments, []);
});

test("derived templates keep the common provenance and privacy fields", () => {
  for (const name of ["decision.md", "pitfall.md", "knowledge.md", "checkpoint.md", "global-preferences.md"]) {
    const fields = frontmatter(name);
    for (const key of ["title", "type", "scope", "project_id", "status", "created", "last_updated", "sensitivity", "sources"]) {
      assert.ok(Object.hasOwn(fields, key), name + ": missing " + key);
    }
    assert.deepEqual(fields.sources, [], "Templates must not contain fabricated sources");
  }
});

test("bootstrap starts with no inferred taxonomy or preferences", () => {
  assert.equal(frontmatter("project-agent.md").schema_state, "pending-first-ingest");
  const preferences = frontmatter("global-preferences.md");
  assert.equal(preferences.status, "uninitialized");
  assert.equal(preferences.scope, "global");
  assert.equal(preferences.project_id, null);
});

test("templates require only known identity and evidence placeholders", () => {
  const allowed = new Set([
    "candidate_id", "title", "scope", "project_id", "project_root", "project_name",
    "created", "last_updated", "captured_at", "source_ref"
  ]);
  for (const name of templates) {
    for (const match of read(name).matchAll(/\{\{([^}]+)\}\}/g)) {
      assert.ok(allowed.has(match[1]), name + ": unknown template variable " + match[1]);
    }
  }
});
