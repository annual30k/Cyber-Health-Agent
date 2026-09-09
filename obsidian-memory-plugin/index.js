import { parseConfigs } from "./lib/config.js";
import { buildGuidance } from "./lib/prompt.js";

// A native OpenClaw entry object. No SDK/runtime dependency or build step.
export default {
  id: "obsidian-memory-plugin",
  name: "Obsidian Memory",
  description: "The memory skill, using the host's existing Obsidian skills.",
  register(api) {
    const configs = parseConfigs(api.pluginConfig ?? {});
    if (configs.size === 0) {
      api.logger?.info?.("Obsidian Memory: configure agentId/vaultPath or agentConfigs to enable guidance.");
      return;
    }
    const guidanceByAgent = new Map([...configs].map(([agentId, config]) => [agentId, buildGuidance(config)]));
    api.on("before_prompt_build", (_event, context) => {
      const guidance = guidanceByAgent.get(context?.agentId);
      if (!guidance) return;
      return { prependSystemContext: guidance };
    });
  }
};
