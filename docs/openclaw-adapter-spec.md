# OpenClaw 适配规范（P0）

**状态：** 审阅基线  
**适用版本：** Cyber Health Core v0.1.x；OpenClaw 当前 MCP Registry 机制  
**范围：** 单用户、本地优先、一个共享 SQLite 事实库、OpenClaw 作为首个宿主。

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

P0 不包含云同步、多用户、多宿主同时写入、远程 HTTP 发布、医疗诊断或将原始图片落盘。

## 2. 会话与状态恢复

每个新建 OpenClaw 会话都视为无状态。它不读取或复用其他聊天的上下文来恢复健康数据。

| 阶段 | OpenClaw 行为 | Cyber Health 约束 |
| --- | --- | --- |
| 会话开始 | 调用 `cyber_health_get_profile` | 返回用户目标、限制和 `state_version` |
| 获取当日状态 | 调用 `cyber_health_get_today` | 从 SQLite 读取已提交事实，不依赖聊天历史 |
| 修改前确认 | 将当前 `state_version` 带入写工具 | 不匹配时返回 `CONFLICT_VERSION` |
| 写入失败后重试 | 复用相同 `idempotency_key` | 返回原操作结果，不产生重复账目 |

写入成功的必要条件是：领域事实、派生账本和审计记录已在同一 SQLite 事务中提交。MemoryProvider 不可用时，写入仍可成功，但响应包含 `MEMORY_DEFERRED` 警告。

## 3. P0 MCP 工具面

P0 只注册以下工具；工具名称、字段和错误码是跨宿主 Core Contract 的一部分。

| 工具 | 类型 | 必填输入 | 成功输出 | 失败 / 限制 |
| --- | --- | --- | --- | --- |
| `cyber_health_get_profile` | 读 | `user_id` | `profile`, `state_version` | 不创建健康事实 |
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

以下是**目标配置形态**；只有在 `cyber_health_mcp` 可执行入口完成后才执行，路径和 Python 解释器必须替换为本机已验证的绝对路径。

```bash
openclaw mcp add cyber-health \
  --command /absolute/path/to/python3 \
  --arg -m \
  --arg cyber_health_mcp \
  --cwd /absolute/path/to/Cyber-Health-Agent \
  --env CYBER_HEALTH_DB=/absolute/path/to/cyber-health.sqlite3 \
  --include 'cyber_health_get_profile,cyber_health_get_today,cyber_health_log_meal,cyber_health_get_audit_trail,cyber_health_health_check,cyber_health_get_schedule'

openclaw mcp doctor cyber-health --probe
openclaw mcp tools cyber-health --include 'cyber_health_get_*,cyber_health_log_meal'
```

不得把数据库路径、健康数据或凭据写入 Git。P0 没有外部 API 凭据；若后续引入远程服务，应使用 OpenClaw 的认证配置，不在命令行硬编码 token。

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

## 8. 验收清单

1. `openclaw mcp doctor cyber-health --probe` 成功并发现恰好六个 P0 工具。
2. 新建两个独立 OpenClaw 会话：会话 A 记录餐食；会话 B 读取同一天数据，余额和 `state_version` 一致。
3. 在 OpenClaw 重发同一工具调用（同一 `idempotency_key`），数据库中仍只有一条 active 餐食和一条 operation log。
4. 让两个会话基于相同旧版本修改同一餐：第一个成功，第二个收到 `CONFLICT_VERSION`，没有静默覆盖。
5. 重启 MCP 服务与 OpenClaw 后重新运行 `probe` 和 `get_today`，事实不丢失。
6. 将一个 `pending` 事件置于过去窗口，下一次 `get_schedule` 返回 `overdue` 补偿；标记为 `acknowledged` 后不再返回。
7. 模拟 MemoryProvider 不可用，餐食写入仍成功且响应包含 `MEMORY_DEFERRED`。

## 9. 实施顺序

1. 完成 MCP transport：输入 JSON Schema、工具注册、统一错误 envelope 与 stdio 生命周期。
2. 执行上述六项验收并保存 `doctor --probe` 输出。
3. 接入 OpenClaw 的实际提醒机制，验证送达回写与补偿。
4. 仅在 P0 稳定后，追加训练、每日复盘、MemoryProvider 写入和第二宿主适配。
