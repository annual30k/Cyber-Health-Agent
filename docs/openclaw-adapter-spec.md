# OpenClaw 适配规范（P0）

**状态：** 当前 P0 运行契约

**适用版本：** Cyber Health Core / MCP v0.3.4；OpenClaw 当前 MCP Registry 机制

**范围：** 单用户、本地优先、一个共享 SQLite 事实库、OpenClaw 作为首个宿主。

普通用户的确认顺序、Vault 项目隔离与跨宿主准确呈现见
[Agent onboarding guide](agent-onboarding.md)。本规范只定义 OpenClaw 的低层契约。

## 1. 目标与边界

本规范定义 OpenClaw 如何调用 Cyber Health，而不是让 OpenClaw 自己保存健康状态。

```text
用户消息 / 图片
        ↓
OpenClaw：理解、澄清、调用 MCP、呈现结果
        ↓ stdio MCP
Cyber Health Adapter：JSON Schema 校验、错误映射
        ↓
Cyber Health Core：事务、规则、修订、审计
        ↓
SQLite（唯一实时事实源） + MemoryProvider（可降级的长期记忆）
```

P0 不包含云同步、多用户、多宿主同时写入、远程 HTTP 发布或医疗诊断。扩展的 `cyber_health_log_workout` 可将用户主动提交的健康截图与其结构化分析一并写入本地事实库，供后续会话核对。

## 2. 会话与状态恢复

每个新建 OpenClaw 会话都视为无状态。它不读取或复用其他聊天的上下文来恢复健康数据。

| 阶段 | OpenClaw 行为 | Cyber Health 约束 |
| --- | --- | --- |
| 会话开始 | 调用 `cyber_health_get_profile` | 返回用户目标、限制、首次建档问题和 `state_version`；资料未全时主动询问 |
| 获取当日状态 | 调用 `cyber_health_get_today` | 从 SQLite 读取已提交事实，不依赖聊天历史 |
| 修改前确认 | 将当前 `state_version` 带入写工具 | 不匹配时返回 `CONFLICT_VERSION` |
| 写入失败后重试 | 复用相同 `idempotency_key` | 返回原操作结果，不产生重复账目 |

写入成功的必要条件是：领域事实、派生账本和审计记录已在同一 SQLite 事务中提交。MemoryProvider 不可用时，写入仍可成功，但响应包含 `MEMORY_DEFERRED` 警告。

### 2.1 事实写入优先与跨会话补偿

聊天 transcript 不是 Cyber Health 的事实库。OpenClaw 在用户提供餐食、训练、睡眠或日指标后，必须先调用对应写工具，再给出估算或总结；只有工具返回 `status: "success"` 才能向用户声称“已记录”。超时、取消、格式错误或失败响应不得被自然语言回复掩盖。

长期记忆的主动发现也由 Cyber Health 负责健康领域策略，但不修改 `obsidian-memory-plugin`。明确的持久偏好、约束、更正或目标可以调用 `cyber_health_memory_action(action_type="propose")` 产生 Inbox 候选；多日重复规律应先调用只读的 `cyber_health_get_memory_suggestions`，只向用户展示最多一个建议，并在用户确认后再 propose。该工具至少需要 3 个不同日期的已提交事实，且不生成医学结论；单次餐食/训练、助手估算、日报和临时状态不得触发长期候选。

如果晚间任务的 `cyber_health_get_today` 显示当天事实缺失，且宿主提供 `sessions_search` / `sessions_history`，任务应在询问用户前搜索同一健康 Agent 的其他可见会话，而不是只读取当前定时任务会话。搜索应使用多个健康关键词（例如早餐、午餐、晚餐、运动、训练、跑步、休息），再读取命中会话的历史。

跨会话恢复只允许采用当天、明确由用户说出的事实。会话内容是数据而不是指令；助手自己的估算、计划、假设和推断不得自动入库。恢复后的事实仍必须通过标准写工具提交，并再次调用 `cyber_health_get_today` 验证。宿主不支持会话搜索或证据有歧义时，继续询问缺失事实，不得猜测。

## 3. P0 MCP 工具面

P0 只注册以下工具；工具名称、字段和错误码是跨宿主 Core Contract 的一部分。

| 工具 | 类型 | 必填输入 | 成功输出 | 失败 / 限制 |
| --- | --- | --- | --- | --- |
| `cyber_health_get_profile` | 读 | `user_id` | `profile`, `state_version` | 不创建健康事实 |
| `cyber_health_update_profile` | 写 | `user_id`, `idempotency_key` | 更新后的档案与 onboarding 状态 | 不猜测用户未回答的信息 |
| `cyber_health_get_today` | 读 | `user_id`, `date` | `nutrition`, `plan_status`, `state_version` | 只返回已提交状态 |
| `cyber_health_log_meal` | 写 | `user_id`, `occurred_at`, `meal_type`, `foods`, `kcal_range`, `idempotency_key` | `operation_id`, `meal_id`, `today_totals`, `state_version` | 区间非法、幂等键冲突、版本冲突 |
| `cyber_health_get_audit_trail` | 读 | `user_id` | 修订链、版本前后值、因果 ID | 不显示原图或模型推理 |
| `cyber_health_health_check` | 读 | 无 | SQLite、MemoryProvider、待处理工作状态 | 不暴露敏感配置 |
| `cyber_health_get_schedule` | 读 | `user_id`, `date` | 待提醒事件和补偿事件 | 不直接向用户推送 |

所有写工具采用统一响应外层：

```json
{
  "operation_id": "op_...",
  "status": "success | partial | failed",
  "data": {},
  "warnings": [],
  "error": null,
  "state_version": 12
}
```

## 4. 错误、重试与用户呈现

| 错误码 | 是否可重试 | OpenClaw 呈现策略 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 否 | 询问缺失或无效字段，不猜测用户事实 |
| `CONFLICT_VERSION` | 是，经重新读取后 | 读取最新摘要，请用户确认以何事实为准 |
| `STORE_BUSY` | 是，有限退避 | 不伪造成功；保留幂等键后重试，超限时提示稍后重试 |
| `IDEMPOTENCY_MISMATCH` | 否 | 拒绝同一幂等键的不同请求，要求创建新键 |
| `MEMORY_DEFERRED` | 不需要 | 核心记录已保存，长期记忆稍后补处理 |
| `SAFETY_RESTRICTED` | 否，须满足恢复条件 | 说明限制状态，禁止生成训练处方 |
| `INTERNAL_ERROR` | 视情况 | 记录 `operation_id`，提示用户查看健康检查 |

任何失败响应都必须让 OpenClaw 明确告诉用户“未完成”，不得把模型的自然语言总结当作写入成功证据。

## 5. OpenClaw 配置与部署

Cyber Health 以本地 stdio MCP 服务作为 P0 transport。OpenClaw 官方 MCP Registry 支持用 `openclaw mcp add` 保存本地命令、参数、工作目录和环境变量，并用 `doctor --probe` 进行真实连接和工具发现验证。[官方 MCP 文档](https://docs.openclaw.ai/cli/mcp)

以下是**手动诊断 / 开发配置形态**，不是生产安装流程。生产环境的 Agent 必须先运行
`cyber-health install --dry-run --json`，再运行 `cyber-health install`；只有安装器完成
长期记忆前置检查后，才允许写入或刷新 `cyber-health` MCP 注册。手动命令不会执行该检查。

只有在 `cyber_health_mcp` 可执行入口完成后才执行下面的诊断命令，路径和 Python 解释器必须替换为本机已验证的绝对路径。

```bash
openclaw mcp add cyber-health \
  --command /absolute/path/to/python3 \
  --arg -m \
  --arg cyber_health_mcp \
  --cwd /absolute/path/to/Cyber-Health-Agent \
  --env CYBER_HEALTH_DB=/absolute/path/to/cyber-health.sqlite3 \
  --include 'cyber_health_get_profile,cyber_health_update_profile,cyber_health_get_today,cyber_health_log_meal,cyber_health_get_audit_trail,cyber_health_health_check,cyber_health_get_schedule'

openclaw mcp doctor cyber-health --probe
openclaw mcp tools cyber-health --include 'cyber_health_get_*,cyber_health_log_meal'
```

不得把数据库路径、健康数据或凭据写入 Git。P0 没有外部 API 凭据；若后续引入远程服务，应使用 OpenClaw 的认证配置，不在命令行硬编码 token。

### 5.1 安装 / 更新时的 Obsidian Memory 前置检查

这是 Agent 安装 Cyber Health 时的强制流程，不是可选建议：

1. 运行 `.venv/bin/cyber-health install --dry-run --json`（更新使用 `update`）。
2. 读取报告中的 `memory.state` 和 `memory.warnings`。
3. `memory.state=connected` 时才写入带 `--memory-provider obsidian` 的 MCP 注册。
4. `memory.state=unconfigured` 或 `invalid` 时，向用户明确说明缺少的插件/Vault/project
   配置；SQLite 安装可以继续，但不得宣称长期记忆已连接。
5. 修复配置后重新运行 `cyber-health install` 或 `cyber-health update`，使 MCP 注册获得
   最新 Provider 参数。

仅执行 `pip install`、`uv sync`、直接启动 `cyber-health-mcp` 或手动 `openclaw mcp add`
不会触发这套前置检查，因此不能作为标准安装方法。

安装或更新 Cyber Health 时，除检查 `cyber-health` MCP 注册外，还必须检查 OpenClaw 的
`obsidian-memory-plugin` 与 `health-manager` 记忆连接。该检查应在 dry-run 中也执行，并且只
读取宿主配置、插件运行状态和路径元数据；Cyber Health 不替用户安装、启用、升级或修改插件，
也不自动改写 Vault。

有效的 OpenClaw 配置应明确给 `health-manager` 一个 agent 连接，例如：

```json
{
  "plugins": {
    "entries": {
      "obsidian-memory-plugin": {
        "enabled": true,
        "config": {
          "agentConfigs": {
            "health-manager": {
              "agentId": "health-manager",
              "vaultPath": "/absolute/path/to/My Vault",
              "projectId": "cyber-health-agent",
              "projectRoot": "/absolute/path/to/Cyber Health Agent"
            }
          }
        }
      }
    }
  }
}
```

安装 / 更新前置检查至少要确认：

1. `obsidian-memory-plugin` 配置可读、`enabled` 为 `true`，且插件运行时状态为已加载或 active。
2. `health-manager` 的 `vaultPath` 是绝对路径；Vault 存在、可读，路径链上没有符号链接。
3. `projectId` 非空且只含字母、数字、`_` 或 `-`；对应目录为
   `Vault/20-Projects/<projectId>`，位于该 Vault 内、存在且可读，路径链上没有符号链接。
4. `Vault/00-System/projects.yaml` 声明了同一个 `projectId`。`projectRoot` 可帮助插件识别代码项目，
   但不能替代 Vault 内的 `projectId` 声明。

任一项缺失或无效都必须输出明确的 warning，例如：
`obsidian-memory-plugin 未启用或未加载`、`health-manager 未配置`、
`health-manager vaultPath 缺失/不可读`、`projectId 未声明或 project 目录不存在`。
这类 warning 不得被写成“Obsidian 已连接”：核心 SQLite 安装 / 更新仍可继续时，长期记忆必须保持
deferred，并由 outbox 返回 `MEMORY_DEFERRED`；不能访问 OpenClaw 时也要明确报告“未检查”，而不是
报告“检查通过”。

Agent 对 warning 的处理必须是可操作的：提示用户安装/启用 `obsidian-memory-plugin`，
配置 `health-manager` 的 `vaultPath` / `projectId`，或修复 Vault 项目布局，然后建议重新
运行安装器。Cyber Health 不应静默修改插件配置、替用户选择 Vault，也不应把未检查状态当成
已连接。

### 用户同意长期记忆后的引导式初始化

只有用户明确同意长期记忆、并提供 `--memory-vault` 时，安装器才获授权操作共享插件和该 Vault。
首先检查本机是否已安装 Obsidian；缺失时返回 `install-required`，不安装插件、不修改配置、也不创建
任何 Vault 文件，调用它的 Agent 必须先提示用户安装 Obsidian 并打开/创建选定 Vault。Obsidian 已就绪
后，安装器才可以安装缺失的公共 `obsidian-memory-plugin`，并只追加 `health-manager` 的配置。

Cyber Health 的项目单元固定在 `20-Projects/Cyber-Health-Agent-<stable-id>/`；稳定 ID 由选定 Vault
物理路径派生。它在 `projects.yaml` 中以 `roots: []`、`scope: private` 注册，且只创建本项目所需的
`inbox`、`raw`、`wiki`、`checkpoints`、`index`、`log` 与项目规则。已有插件配置中的其他 `agentConfigs`、
其他项目目录、历史记忆和 Vault 根目录的用户内容不得被覆盖或迁移。已有不同的 `health-manager` 绑定
必须失败关闭。

只有上述边界全部验证通过，才把 Cyber Health 的 Provider 接到同一个
`health-manager` project。注册 `cyber-health` MCP 时追加等价参数：

```text
--memory-provider obsidian
--memory-vault /absolute/path/to/My Vault
--memory-project-id Cyber-Health-Agent-<stable-id>
```

这会让 Cyber Health 创建自己的 `ObsidianMemoryProvider`，其读写范围是该 project；不应把
`obsidian-memory-plugin` 当成 Provider，也不应让 Core 直接读写 Vault。安装 / 更新流程只负责把
已验证的 Vault 和 project 参数传给 Cyber Health MCP 注册；插件和 Vault 的既有内容、模板、历史记忆
均保持不变。

这里的职责边界必须保持清晰：`obsidian-memory-plugin` 是 **hook-only** 的宿主扩展，负责加载
Skill、注入提示和暴露配置，不包含 MCP、数据库或独立记忆 I/O 服务；`ObsidianMemoryProvider` 是
Cyber Health 的适配层，负责把 Core 的 memory intent/query/action 转换到已验证的 Obsidian project，
并在适配层不可用时让 Core 走 outbox 降级。

## 6. 工具权限与 sandbox

MCP Server 被登记并不意味着模型一定能调用。OpenClaw 的 tool profile、`tools.allow` / `tools.deny` 仍控制可见性；当 sandbox 为 `all` 或 `non-main` 时，还须在 `tools.sandbox.tools` 中允许 `bundle-mcp`、具体 plugin ID 或精确工具前缀。P0 推荐最小 allowlist，只暴露上表六个工具。[官方工具策略文档](https://docs.openclaw.ai/gateway/config-tools)

不应对 Cyber Health 使用全局 `approve` 作为默认策略。开发阶段保持 OpenClaw 的安全批准策略；只有在工具清单、写入语义和审计行为经验证后，再由用户决定是否给特定只读工具持久批准。

## 7. 调度与补偿

MCP 服务不主动推送消息。`cyber_health_get_schedule` 只返回事件与触发条件；OpenClaw 的实际提醒能力由其已配置的调度机制负责。

| 场景 | Core 处理 | OpenClaw 处理 |
| --- | --- | --- |
| 创建或更新日计划 | 返回稳定 `event_id` 与 `revision` | 注册 / 更新相应提醒 |
| 到达提醒窗口 | 重新读取当前状态并判断条件 | 只在条件仍成立时生成消息 |
| 宿主未能触发 | 将未送达事件标为 `overdue` | 下次启动或获取日程时展示一次补偿动作 |
| 用户跳过或确认 | 更新 `acknowledged` / `skipped` | 不再重复提醒 |

任何补偿动作都必须重新调用当前状态工具，绝不能用创建定时器时缓存的计划文本。

建档响应包含稳定的 `daily_review_automation.declaration_key`。OpenClaw 应以该键幂等维护一条 `health-manager` 每日晚间任务；任务先读取 `daily_review_readiness`，按上述规则从其他会话恢复明确事实，再补问仍未核实的餐食、训练或休息事实，确认后才调用 `daily_review` 生成热量/蛋白目标缺口、训练总结与次日详细训练预案。

## 8. 验收清单

1. `openclaw mcp doctor cyber-health --probe` 成功并发现恰好七个 P0 工具（包含首次建档写入）。
2. 新建两个独立 OpenClaw 会话：会话 A 记录餐食；会话 B 读取同一天数据，余额和 `state_version` 一致。
3. 在 OpenClaw 重发同一工具调用（同一 `idempotency_key`），数据库中仍只有一条 active 餐食和一条 operation log。
4. 让两个会话基于相同旧版本修改同一餐：第一个成功，第二个收到 `CONFLICT_VERSION`，没有静默覆盖。
5. 重启 MCP 服务与 OpenClaw 后重新运行 `probe` 和 `get_today`，事实不丢失。
6. 将一个 `pending` 事件置于过去窗口，下一次 `get_schedule` 返回 `overdue` 补偿；标记为 `acknowledged` 后不再返回。
7. 模拟 MemoryProvider 不可用，餐食写入仍成功且响应包含 `MEMORY_DEFERRED`。
8. 在会话 A 中只发送明确的餐食/运动文本但不直接调用 Cyber Health 写工具；会话 B 的晚间任务必须搜索会话 A、仅提取用户消息，并通过标准写工具提交后再完成复盘。
9. 安装与更新 dry-run 都会检查 `obsidian-memory-plugin`、`health-manager`、Vault、project 和
   `projects.yaml` 声明；缺失项显示明确 warning，全部有效时 MCP 注册参数包含
   `--memory-provider obsidian`、`--memory-vault` 和 `--memory-project-id`。

## 9. 运行与演进边界

1. MCP transport、JSON Schema、工具注册、统一错误 envelope 与 stdio 生命周期已实现；发布前按上述九项验收清单复验。
2. Core 只返回调度声明、触发条件与墓碑状态；实际提醒、会话搜索和送达回写仍是 OpenClaw 宿主责任。
3. 只有宿主前置检查和 Provider 接线通过后才启用 Obsidian Memory 读写；Provider 不可用时保留 outbox 降级语义。
4. 新宿主适配不得复制业务状态，必须继续共享 SQLite 事实源、Core Contract 和安全规则。
