import { parseConfig } from "./lib/config.js";
import { buildGuidance } from "./lib/prompt.js";

// A native OpenClaw entry object. No SDK/runtime dependency or build step.
export default {
  id: "obsidian-memory-plugin",
  name: "Obsidian Memory",
  description: "The memory skill, using the host's existing Obsidian skills.",
  register(api) {
    const config = parseConfig(api.pluginConfig ?? {});
    if (!config) {
      api.logger?.info?.("Obsidian Memory: configure agentId and vaultPath to enable guidance.");
      return;
    }
    const guidance = buildGuidance(config);
    api.on("before_prompt_build", (_event, context) => {
      if (context?.agentId !== config.agentId) return;
      return { prependSystemContext: guidance };
    });
  }
};
