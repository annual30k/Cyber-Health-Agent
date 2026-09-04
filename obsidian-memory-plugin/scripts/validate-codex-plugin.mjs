import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const manifestPath = resolve(root, ".codex-plugin", "plugin.json");
const failures = [];

function requireString(value, label) {
  if (typeof value !== "string" || !value.trim()) failures.push(`${label} must be a non-empty string`);
}

function requireDirectory(path, label) {
  if (!existsSync(path) || !statSync(path).isDirectory()) failures.push(`${label} must be an existing directory`);
}

let manifest;
try {
  manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
} catch (error) {
  failures.push(`cannot read valid JSON from .codex-plugin/plugin.json: ${error.message}`);
}

if (manifest) {
  requireString(manifest.name, "name");
  requireString(manifest.version, "version");
  requireString(manifest.description, "description");
  requireString(manifest.author?.name, "author.name");
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(manifest.version ?? "")) {
    failures.push("version must use semantic versioning");
  }
  if (typeof manifest.skills !== "string" || !manifest.skills.startsWith("./")) {
    failures.push("skills must be a plugin-relative path");
  } else {
    requireDirectory(resolve(root, manifest.skills), "skills");
  }
  for (const field of ["displayName", "shortDescription", "longDescription", "developerName", "category"]) {
    requireString(manifest.interface?.[field], `interface.${field}`);
  }
  if (!Array.isArray(manifest.interface?.capabilities) || manifest.interface.capabilities.some(value => typeof value !== "string" || !value.trim())) {
    failures.push("interface.capabilities must be an array of non-empty strings");
  }
  if (!Array.isArray(manifest.interface?.defaultPrompt) || manifest.interface.defaultPrompt.length === 0) {
    failures.push("interface.defaultPrompt must be a non-empty array");
  }
  if (JSON.stringify(manifest).includes("[TODO:")) failures.push("manifest contains an unfinished TODO placeholder");
}

if (failures.length) {
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log(`Codex manifest validation passed: ${manifestPath}`);
}
