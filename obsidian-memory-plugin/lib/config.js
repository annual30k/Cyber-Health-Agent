import { isAbsolute } from "node:path";

const keys = new Set(["agentId", "vault", "vaultPath", "cliPath", "projectId", "projectRoot"]);
const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

function text(value, field, maxLength = 4096) {
  if (typeof value !== "string" || value.length === 0 ||
      value.length > maxLength || value.trim() !== value ||
      /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new TypeError("Obsidian Memory: invalid " + field);
  }
  return value;
}

function absolute(value, field) {
  const result = text(value, field);
  if (!isAbsolute(result)) throw new TypeError("Obsidian Memory: " + field + " must be an absolute path");
  return result;
}

export function parseConfig(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("Obsidian Memory: config must be an object");
  }
  const fields = Object.keys(input);
  if (fields.length === 0) return null;
  for (const field of fields) {
    if (!keys.has(field)) throw new TypeError("Obsidian Memory: unknown config field");
  }
  const config = {
    agentId: text(input.agentId, "agentId", 128),
    vaultPath: absolute(input.vaultPath, "vaultPath"),
    cliPath: hasOwn(input, "cliPath") ? text(input.cliPath, "cliPath") : "obsidian"
  };
  if (hasOwn(input, "vault")) config.vault = text(input.vault, "vault", 256);
  if (config.cliPath !== "obsidian" && !isAbsolute(config.cliPath)) {
    throw new TypeError("Obsidian Memory: cliPath must be obsidian or an absolute executable path");
  }
  if (hasOwn(input, "projectId")) {
    config.projectId = text(input.projectId, "projectId", 128);
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/u.test(config.projectId)) {
      throw new TypeError("Obsidian Memory: invalid projectId");
    }
  }
  if (hasOwn(input, "projectRoot")) {
    config.projectRoot = absolute(input.projectRoot, "projectRoot");
  }
  return Object.freeze(config);
}

export function parseConfigs(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("Obsidian Memory: config must be an object");
  }
  if (!hasOwn(input, "agentConfigs")) {
    const config = parseConfig(input);
    return config ? new Map([[config.agentId, config]]) : new Map();
  }
  if (Object.keys(input).length !== 1 || !input.agentConfigs || typeof input.agentConfigs !== "object" || Array.isArray(input.agentConfigs)) {
    throw new TypeError("Obsidian Memory: agentConfigs must be the only config field");
  }
  const entries = Object.entries(input.agentConfigs);
  if (entries.length === 0) throw new TypeError("Obsidian Memory: agentConfigs must not be empty");
  return new Map(entries.map(([agentId, connection]) => [agentId, parseConfig({ ...connection, agentId })]));
}
