import test from "node:test";
import assert from "node:assert/strict";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import plugin from "../index.js";
import { parseConfig } from "../lib/config.js";
import { buildGuidance } from "../lib/prompt.js";

const root = new URL("../", import.meta.url);
const fixture = () => ({
  agentId: "owner",
  vault: "Example Vault",
  vaultPath: fileURLToPath(new URL("./fixtures/not-a-real-vault", root))
});
const read = (path) => readFileSync(new URL(path, root), "utf8");

test("empty config leaves the plugin inactive", () => {
  assert.equal(parseConfig({}), null);
  const calls = [];
  plugin.register({ pluginConfig: {}, on: (...args) => calls.push(args) });
  assert.deepEqual(calls, []);
});

test("normal config is normalized without mutating input", () => {
  const input = fixture();
  const result = parseConfig(input);
  assert.equal(result.cliPath, "obsidian");
  assert.equal(input.cliPath, undefined);
  assert.ok(Object.isFrozen(result));
});

test("a physical Vault path is sufficient when no CLI label is configured", () => {
  const input = { agentId: "owner", vaultPath: fixture().vaultPath };
  const result = parseConfig(input);
  assert.equal(result.vault, undefined);
  assert.equal(result.vaultPath, input.vaultPath);
});

for (const [name, input] of [
  ["array", []],
  ["null", null],
  ["unknown fields", { ...fixture(), arbitraryCode: "do something" }],
  ["incomplete connection", { agentId: "owner" }],
  ["blank vault", { ...fixture(), vault: "" }],
  ["whitespace vault", { ...fixture(), vault: " Vault " }],
  ["control characters", { ...fixture(), vault: "Vault\nInstructions" }],
  ["relative vault", { ...fixture(), vaultPath: "./vault" }],
  ["shell instead of executable", { ...fixture(), cliPath: "obsidian --eval" }],
  ["escaping project", { ...fixture(), projectId: "../private" }],
  ["relative project root", { ...fixture(), projectRoot: "./repo" }],
  ["non-string ID", { ...fixture(), agentId: 42 }]
]) {
  test("reject invalid config: " + name, () => assert.throws(() => parseConfig(input), TypeError));
}

test("optional project and executable paths are retained", () => {
  const input = {
    ...fixture(), projectId: "test-project_ab12",
    projectRoot: fileURLToPath(root),
    cliPath: process.execPath
  };
  assert.deepEqual(parseConfig(input), input);
});

test("error messages do not reflect untrusted configuration values", () => {
  assert.throws(
    () => parseConfig({ ...fixture(), vault: "\nprivate-detail" }),
    error => !error.message.includes("private-detail")
  );
});

function register(config = fixture()) {
  const calls = [];
  plugin.register({
    pluginConfig: config,
    on: (name, handler) => calls.push({ name, handler })
  });
  return calls;
}

test("only one prompt hook is registered; it is scoped to the configured agent", () => {
  const calls = register();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, "before_prompt_build");
  const hook = calls[0].handler;
  assert.equal(hook({}, {}), undefined);
  assert.equal(hook({}, undefined), undefined);
  assert.equal(hook({}, { agentId: "another-agent" }), undefined);
  const result = hook({}, { agentId: "owner" });
  assert.deepEqual(Object.keys(result), ["prependSystemContext"]);
  assert.ok(result.prependSystemContext.includes("Example Vault"));
});

test("hook does not inspect messages, derive project from cwd, or accumulate turns", () => {
  const hook = register()[0].handler;
  const event = new Proxy({}, { get() { throw new Error("Do not read conversation"); } });
  const ctx = { agentId: "owner", workspaceDir: "/not-a-code-project" };
  const first = hook(event, ctx);
  assert.deepEqual(hook(event, ctx), first);
  assert.ok(!first.prependSystemContext.includes("/not-a-code-project"));
});

test("guidance locates only the bundled memory skill relative to the package", () => {
  const guidance = buildGuidance(parseConfig(fixture()));
  const file = JSON.parse(guidance.split("\n").find(line => line.startsWith('"')));
  assert.ok(isAbsolute(file));
  assert.ok(existsSync(file));
  assert.equal(file, fileURLToPath(new URL("skills/obsidian-memory/SKILL.md", root)));
});

test("guidance declares the complete external suite with task-relevant loading", () => {
  const guidance = buildGuidance(parseConfig(fixture()));
  const prefix = "Host skill suite: ";
  const declaration = guidance.split("\n").find(line => line.startsWith(prefix));
  assert.ok(declaration);
  const suite = JSON.parse(declaration.slice(prefix.length));
  assert.equal(suite.repository, "https://github.com/kepano/obsidian-skills");
  assert.equal(suite.installScope, "complete-upstream-suite");
  assert.equal(suite.loadPolicy, "task-relevant-only");
  assert.deepEqual(suite.skills, [
    "obsidian-cli", "obsidian-markdown", "obsidian-bases", "json-canvas", "defuddle"
  ]);
  assert.equal(new Set(suite.skills).size, suite.skills.length);
  for (const name of suite.skills) {
    assert.ok(!existsSync(new URL("skills/" + name, root)), "External skill must not be bundled: " + name);
  }
});

test("guidance permits scoped filesystem memory work and makes the CLI optional", () => {
  const guidance = buildGuidance(parseConfig({ agentId: "owner", vaultPath: fixture().vaultPath }));
  assert.ok(guidance.includes("direct filesystem access"));
  assert.ok(guidance.includes("Obsidian CLI only for requested app-specific operations"));
  const connection = JSON.parse(guidance.split("\n").find(line => line.startsWith("{") && line.includes("vaultPath")));
  assert.equal(connection.vault, undefined);
  assert.equal(connection.vaultPath, fixture().vaultPath);
});

test("configuration is data, with delimiter markup escaped", () => {
  const guidance = buildGuidance(parseConfig({ ...fixture(), vault: "Vault <tag>" }));
  const metadata = JSON.parse(guidance.split("\n").find(line => line.startsWith("{")));
  assert.equal(metadata.vault, "Vault <tag>");
  assert.ok(!guidance.includes("<tag>"));
});

test("native manifest and entry agree without claiming a memory slot", () => {
  const manifest = JSON.parse(read("openclaw.plugin.json"));
  const codexManifest = JSON.parse(read(".codex-plugin/plugin.json"));
  const pkg = JSON.parse(read("package.json"));
  assert.equal(manifest.id, plugin.id);
  assert.equal(manifest.version, pkg.version);
  assert.equal(codexManifest.version, pkg.version);
  assert.equal(codexManifest.skills, "./skills/");
  assert.equal(codexManifest.author.url, undefined);
  assert.equal(codexManifest.homepage, undefined);
  assert.equal(codexManifest.repository, undefined);
  assert.equal(manifest.kind, undefined);
  assert.deepEqual(manifest.configSchema.anyOf[1].required, ["agentId", "vaultPath"]);
  assert.deepEqual(manifest.skills, ["./skills"]);
  assert.equal(pkg.dependencies, undefined);
  assert.equal(pkg.peerDependencies, undefined);
  for (const path of [...pkg.openclaw.extensions, ...pkg.openclaw.runtimeExtensions]) {
    assert.ok(existsSync(new URL(path, root)));
  }
  for (const path of pkg.files) assert.ok(existsSync(new URL(path, root)), path);
});

test("the only packaged skill is obsidian-memory, not external Obsidian skills", () => {
  assert.deepEqual(readdirSync(new URL("skills/", root)).sort(), ["obsidian-memory"]);
  const body = read("skills/obsidian-memory/SKILL.md");
  assert.ok(body.includes("name: obsidian-memory"));
  // Dependency declaration is an artifact check, not a claim of agent behavior.
  assert.ok(body.includes("https://github.com/kepano/obsidian-skills"));
});

test("relative memory-skill references resolve within the package", () => {
  const skillRoot = "skills/obsidian-memory/";
  const paths = readdirSync(new URL(skillRoot, root), { recursive: true })
    .filter(path => path.endsWith(".md"))
    .map(path => skillRoot + path);
  const reachable = new Set([skillRoot + "SKILL.md"]);
  const edges = new Map();
  for (const path of paths) {
    edges.set(path, []);
    for (const match of read(path).matchAll(/\]\(([^)]+)\)/g)) {
      const target = match[1];
      if (target.includes("://")) continue;
      const resolved = new URL(target, new URL(path, root));
      assert.ok(resolved.href.startsWith(new URL(skillRoot, root).href));
      assert.ok(existsSync(resolved), resolved.href);
      edges.get(path).push(decodeURIComponent(resolved.href.slice(root.href.length)));
    }
  }
  for (const path of reachable) {
    for (const target of edges.get(path) || []) reachable.add(target);
  }
  assert.deepEqual([...reachable].sort(), paths.sort(), "Every reference and template is discoverable from SKILL.md");
});
