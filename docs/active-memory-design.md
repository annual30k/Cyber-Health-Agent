# Cyber Health 主动长期记忆设计

**状态：** 已实现；真实 Vault 读写取决于宿主预检查后的 Provider 连接

**目标：** 在不破坏 `obsidian-memory` 自生长规则的前提下，让 Cyber Health 能够主动发现值得长期保留的健康偏好、约束和稳定规律。

**硬约束：** 本设计不修改、不升级、不重打包 `obsidian-memory-plugin`。所有新增代码、触发策略、候选建议和测试均属于 Cyber Health；该插件继续保持现有的 hook-only 实现。

## 1. 设计原则

主动记忆的“主动”指主动发现并提出候选，不指未经确认自动写入最终 Wiki。

```text
已提交的健康事实
        │
        ├── 日常事实 ───────────────→ SQLite
        │
        └── 长期价值评估
                │
                ├── 明确且持久的用户陈述 → 自动捕获到 Inbox
                ├── 多次确认的稳定规律   → 主动向用户提议
                └── 一次性/推断内容       → 不触发
                                            │
                                    用户确认整理
                                            │
                              Raw → Wiki → index/log
```

必须保持以下边界：

1. SQLite 是饮食、训练、睡眠和每日指标的实时事实源。
2. `obsidian-memory-plugin` 提供通用的记忆选择和摄入规则；它仍然是 hook-only，不负责监听健康数据或调用 Cyber Health 工具，且不在本项目主动记忆增强范围内修改。
3. Cyber Health 负责健康领域的候选触发和证据聚合。
4. `ObsidianMemoryProvider` 只负责把已授权的候选/query/action 接到 `health-manager` 项目。
5. Inbox 候选不是已确认知识；只有明确摄入后才能进入 Raw/Wiki。

## 2. 候选触发规则

### 2.1 直接触发：一次即可捕获

用户明确表达以下内容时，Agent 可以自动调用 `cyber_health_memory_action(action_type="propose")`，将候选放入 Inbox。下面的文字只是自然语言示例，不是固定关键词或固定餐别：

- 长期偏好：用户说明希望长期保持某种饮食、训练或作息方式，或明确表示某类内容不适合自己。
- 稳定约束：过敏、明确饮食禁忌、器械限制、固定训练时间。
- 长期目标：“接下来长期以……为目标”。
- 用户纠正的持久事实：“刚才记错了，我一直是……”。
- 用户明确说“记住这个”“以后按这个来”。
- 对未来健康决策有持续影响的重要决定或未完成交接。

候选必须保留用户原话或明确事实，不得把助手推断改写成用户陈述。

### 2.2 模式触发：先提议，不直接摄入

对已经提交到 SQLite 的事实做只读聚合。只有同时满足证据阈值，才主动提示用户是否形成长期记忆候选：

| 模式 | 最小证据 | 允许表达 | 禁止表达 |
| --- | --- | --- | --- |
| 重复餐食/饮食偏好 | 至少 3 个不同日期、14 天内 | “你在这段时间多次选择……” | “这证明你适合……” |
| 稳定训练习惯 | 至少 3 次已记录训练、30 天内 | “你通常在……训练……” | “你应该永远这样训练……” |
| 稳定时间/节奏 | 至少 3 个不同日期 | “你的记录显示通常在……” | 把偶然时间当成规则 |
| 安全限制变化 | 用户明确陈述一次即可 | “你明确说过……” | 由症状或模型分析推断诊断 |

模式候选必须带上证据记录 ID、日期窗口和出现次数。单日重复多次不能伪装成跨日期稳定规律。

### 2.3 明确不触发

- 单次餐食、单次训练、单日复盘全文；
- 助手的热量估算、训练建议、诊断或推测；
- 缺失数据、未确认的跨会话内容；
- 临时状态，例如今天很累、今天想休息；
- 普通工具调用、测试、安装和代码修改；
- 已存在的同义 Wiki/Raw 内容。

## 3. 触发频率和去重

- 每个普通会话最多主动提出 1 个候选。
- 每个自然日最多自动提出 3 个候选。
- 同一 `candidate_key` 在 7 天内不重复提议；用户拒绝后 30 天内不重复提议，除非出现新的明确纠正或安全信息。
- 以用户时区计算日期窗口。
- 只读取 `status=active` 的已提交 SQLite 事实，排除 deleted/superseded 记录。
- 提议必须是可执行的短句，避免把完整日报复制到长期库。

## 4. 用户交互和生命周期

### 4.1 明确陈述

```text
用户：我以后训练日的饮食安排希望更简单一点，记住这个。
Agent：我会把这个长期偏好作为记忆候选放入 Inbox，之后需要整理确认才会进入正式知识库。
```

此时只执行 `propose`，不执行 `confirm`。

### 4.2 模式提议

```text
Agent：我发现你最近 14 天有 3 个不同日期都重复出现了相似的饮食安排。
      要不要把这个重复模式作为长期规律保存？
```

用户同意后才执行 `propose`；用户随后明确要求整理/摄入时才执行 `confirm`。如果当前产品希望减少一步，也只能把用户对该提议的明确同意视为摄入授权，不能把 Agent 自己的判断视为确认。

### 4.3 正式摄入

Provider 在确认后必须完成：

`cyber_health_memory_action` 的 `confirmed=True` 只表示宿主已获得用户确认，不能单独证明用户身份或同意；宿主必须先取得用户对该候选的明确指令。适配器会拒绝缺少该标志的升格动作，并拒绝用内层 `payload.action_type` 改写外层动作。通用 `action` 调用若在 payload 中指定 `confirm`，同样需要顶层 `confirmed=True`。

1. 在 `raw/` 写入不可变证据记录；
2. 在 `wiki/knowledge/` 更新或创建长期知识；
3. 更新项目 `index.md`；
4. 向 `log.md` 追加 `ingest` 记录；
5. 读回并验证结果后，才将 Inbox 候选标记为 `ingested`。

Provider 不可用时，候选保留在 SQLite `memory_outbox`，返回 `MEMORY_DEFERRED`，不得声称已经写入 Obsidian。

## 5. Cyber Health 接口设计

已实现纯读工具：

```text
cyber_health_get_memory_suggestions(
  user_id,
  date,
  window_days=30,
  limit=3
)
```

该工具只读取 SQLite，返回候选建议，不写 SQLite、不写 Vault、不自动确认：

```json
{
  "status": "success",
  "suggestions": [
    {
      "candidate_key": "meal-pattern:<meal-type>:...",
      "kind": "repeated_meal_pattern",
      "statement": "最近 14 天有 3 个不同日期出现相似饮食选择",
      "evidence_count": 3,
      "evidence_window": ["2026-09-01", "2026-09-10"],
      "source_record_ids": ["meal_...", "meal_...", "meal_..."],
      "requires_user_confirmation": true
    }
  ],
  "warnings": []
}
```

现有 `cyber_health_memory_action` 继续负责候选的 `propose`、`confirm`、`reject` 和 outbox 补偿；不新增第二套 Vault 写入协议。

## 6. Agent 工作流

1. 会话开始：读取 profile，并按需查询已确认的长期记忆。
2. 用户提供饮食/训练事实：先写 SQLite；不因单条事实触发长期记忆。
3. 用户明确表达持久偏好/约束：立即捕获候选到 Inbox。
4. 会话即将结束或晚间复盘完成后：调用 `get_memory_suggestions`，最多展示一个高价值候选。
5. 用户同意：调用 `memory_action(propose)`；用户要求整理时调用 `memory_action(confirm)`。
6. 下一次会话：只查询已进入 Wiki/Raw/Checkpoints 的长期内容，不把 Inbox 当作已确认知识。
7. 任一步失败：报告真实状态，并依靠 `memory_outbox` 重试。

晚间日报不得等待长期记忆候选完成；日报的生成仍以 SQLite 当日事实为准。

## 7. 已实现组件

### Agent 触发策略

- Cyber Health MCP host instructions 已包含直接触发、模式提议和禁止触发规则。
- `propose` 与 `confirm` 的授权边界已明确。
- 主动候选已设置会话/日期上限。

### 确定性建议引擎

- 已实现 `cyber_health_get_memory_suggestions`。
- 已加入饮食、训练和时间模式的证据聚合。
- 已加入候选 key、证据窗口、去重和不同日期约束。

### 反馈和运维

- 已记录用户拒绝/确认的候选反馈与冷却状态。
- `health_check` 和 `maintain_memory` 已报告候选及 outbox 状态。
- 已覆盖跨会话、重复候选、Provider 不可用和部分摄入恢复测试。

## 8. 当前实现

Cyber Health 现在提供只读工具 `cyber_health_get_memory_suggestions`。它只读取 SQLite 中
`active` 的餐食和有实际完成证据的训练事实，并按用户时区识别至少 3 个不同日期的重复模式；
计划动作、空训练打卡和未确认的活动摘要不算完成证据。默认最多返回 3 个候选，并按当天已
提出的候选数扣减剩余预算。它不会调用 Provider、不会写 Inbox、不会提升 `state_version`，也不会把单次记录、
助手估算、日报或临时状态生成候选。返回的 `propose_payload` 可交给
`cyber_health_memory_action(action_type="propose")`，但宿主必须先向用户展示并获得确认；
`memory.confirm` 仍只允许用户明确要求后执行。

明确的持久偏好/约束/更正/目标仍由 Agent 根据本文件第 3 节直接提出 Inbox 候选；重复规律则
走“只读建议 → 用户确认 → propose → provider 摄入”的路径。同一候选通过本地 outbox 施加
7 天建议冷却，拒绝候选的冷却为 30 天（前提是拒绝请求携带同一个 `candidate_key`）。Core
每个用户每天最多允许 3 条候选进入 propose 预算；宿主每个会话应使用 `limit=1`，因此同一
会话最多展示一条。

## 9. 验收标准

- 用户说“以后/记住/我不吃/我固定”时，能产生 Inbox 候选，但不会直接写 Wiki。
- 同一规律满足阈值时能主动提议，未满足时不打扰用户。
- 单次饮食、单次训练、计划训练、空打卡和日报不会生成长期候选。
- 用户拒绝后不会在冷却期重复骚扰。
- 用户确认后 Raw、Wiki、index、log 均可读回验证。
- Provider 不可用时 SQLite 事实仍成功，候选进入 outbox 并返回 `MEMORY_DEFERRED`。
- `obsidian-memory-plugin` 代码、manifest、打包产物和安装状态均未被 Cyber Health 修改；它仍保持 hook-only，不被 Cyber Health 当成 MCP Provider。
