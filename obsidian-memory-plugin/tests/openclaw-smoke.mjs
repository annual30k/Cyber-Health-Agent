// Run explicitly after npm pack. Uses only an isolated state/config and no live Gateway.
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { execFileSync } from "node:child_process";

if (!process.argv[2]) throw new Error("Usage: node tests/openclaw-smoke.mjs <package.tgz>");
const archive = resolve(process.argv[2]);
const sandbox = await mkdtemp(join(tmpdir(), "obsidian-memory-smoke-"));
try {
  const unpacked = join(sandbox, "unpacked");
  const state = join(sandbox, "state");
  const configPath = join(state, "openclaw.json");
  await mkdir(unpacked);
  await mkdir(state);
  execFileSync("tar", ["-xzf", archive, "-C", unpacked], { timeout: 15000 });
  const packageRoot = join(unpacked, "package");
  const packedManifest = JSON.parse(await readFile(join(packageRoot, "openclaw.plugin.json"), "utf8"));
  await writeFile(configPath, JSON.stringify({
    plugins: {
      allow: ["obsidian-memory-plugin"],
      load: { paths: [packageRoot] },
      entries: {
        "obsidian-memory-plugin": {
          enabled: true,
          hooks: { allowConversationAccess: true, allowPromptInjection: true },
          config: {
            agentId: "owner",
            vault: "Smoke Test (not connected)",
            vaultPath: join(sandbox, "not-a-real-vault")
          }
        }
      }
    }
  }));
  const env = {
    ...process.env,
    OPENCLAW_STATE_DIR: state,
    OPENCLAW_CONFIG_PATH: configPath
  };
  const binary = process.env.OPENCLAW_BIN || "openclaw";
  const run = (args) => execFileSync(binary, args, {
    env, cwd: sandbox, encoding: "utf8",
    timeout: 30000, maxBuffer: 4 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"]
  });
  const version = run(["--version"]).trim();
  const raw = run(["plugins", "inspect", "obsidian-memory-plugin", "--runtime", "--json"]);
  const offset = raw.indexOf("{");
  assert.ok(offset >= 0, "Expected JSON plugin inspection");
  const report = JSON.parse(raw.slice(offset));
  assert.equal(report.plugin?.id, "obsidian-memory-plugin");
  assert.equal(report.plugin.version, packedManifest.version);
  assert.equal(report.plugin.status, "loaded", JSON.stringify(report));
  assert.deepEqual(report.typedHooks?.map(hook => hook.name), ["before_prompt_build"]);
  assert.deepEqual(report.tools, []);
  assert.deepEqual(report.services, []);
  assert.deepEqual(report.mcpServers, []);
  assert.ok(!report.diagnostics?.some(item => item.level === "error" || item.severity === "error"));
  const skills = run(["skills", "info", "obsidian-memory", "--json"]);
  const skillInfo = JSON.parse(skills.slice(skills.indexOf("{")));
  assert.equal(skillInfo.name, "obsidian-memory");
  assert.equal(skillInfo.eligible, true);
  assert.equal(skillInfo.modelVisible, true);
  // OpenClaw materializes plugin skills in its state directory; prove the
  // exposed copy and its relative references match the packed source exactly.
  const packedSkill = join(packageRoot, "skills", "obsidian-memory");
  const skillFiles = (await readdir(packedSkill, { recursive: true })).filter(path => path.endsWith(".md"));
  for (const relative of skillFiles) {
    assert.equal(
      await readFile(join(skillInfo.baseDir, relative), "utf8"),
      await readFile(join(packedSkill, relative), "utf8")
    );
  }
  console.log(JSON.stringify({
    version, pluginVersion: report.plugin.version, pluginId: report.plugin.id, status: report.plugin.status,
    hook: "before_prompt_build",
    skillSource: "extracted tarball",
    verifiedSkillFiles: skillFiles.length,
    liveGatewayChanged: false, vaultAccessed: false
  }, null, 2));
} finally {
  await rm(sandbox, { recursive: true, force: true });
}
