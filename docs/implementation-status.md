# Cyber Health Agent: 实施与交付状态报告（终验收口）

**文档状态：** 最终交付复验已完成。依据 `docs/codex-final-delivery-review.md`，统一文档与代码版本为实际构建的 v0.2.1（消除文档 0.2.2 与实际 0.2.1 差异）；明确全量 130 项自动化测试由 89 项 Codex 审查驱动测试与 41 项 Gemini 领域契约与回归测试组成，不虚称全部需求均有独立审核；更新真实能力边界声明，明确 Core 与 MCP 在当前规格范围内测试完成，真实外部依赖（Obsidian Vault、宿主主动推屏、临床医师审阅、多模态视觉）未真实连接；全测试（130/130 PASS）、`compileall`、`uv lock --check`、`uv build`（产出 0.2.1 sdist/wheel）及隔离 OpenClaw 探针（`ok: true, issues: []`）全绿通过。  
**基线版本：** Cyber Health Core v0.2.1 / MCP Server v0.2.1 / Package v0.2.1（版本文件、`pyproject.toml`、构建产物与 `uv.lock` 严格统一对齐）  
**适用规范：** `Cyber_Health_Agent_产品与技术规格说明书.md` 与 `docs/openclaw-adapter-spec.md`  
**审核依据：** `docs/codex-final-delivery-review.md` 与 `docs/codex-review-round14.md`

---

## 1. 规格覆盖度与真实能力矩阵（Truth in Advertising）

### 1.1 架构分层真实落地状态（明确 MCP 层与 Core 层边界）

> [!IMPORTANT]
> **真实能力边界声明**：系统严格遵循分层架构设计，真实接口与能力暴露状态如下：

| 架构层级 | 接口/模块 | 真实暴露状态 | 说明与边界 |
| :--- | :--- | :--- | :--- |
| **MCP stdio 服务 (默认)** | 6 个 P0 工具 | **已完成 (Verified)** | 仅暴露 `cyber_health_get_profile`, `cyber_health_get_today`, `cyber_health_log_meal`, `cyber_health_get_audit_trail`, `cyber_health_health_check`, `cyber_health_get_schedule`。全部配置精准 `ToolAnnotations`。 |
| **MCP stdio 服务 (扩展)** | 26 个全量领域工具 (`--allow-all`) | **已完成 (Verified)** | 在 6 个 P0 基础上接通全部 20 个扩展工具：`update_profile`, `log_daily_metrics`, `delete_meal`, `log_workout`, `daily_review`, `plan_tomorrow`, `acknowledge_schedule_event`, `maintain_memory`, `get_remaining_calories`, `get_training_plan`, `complete_workout`, `confirm_training_progression`, `substitute_exercise`, `query_knowledge`, `export_data`, `import_data`, `memory_action`, `schedule_daily_reminders`, `update_schedule_event`, `query_memory`。 |
| **真实安全注解 (ToolAnnotations)** | 细粒度安全声明 | **已完成 (Verified)** | 只读接口配置 `readOnlyHint=True`；破坏性接口（如 `delete_meal`, `maintain_memory`, `import_data`, `memory_action`）配置 `destructiveHint=True`；外部交互接口配置 `openWorldHint=True`；写操作设为 `readOnlyHint=False` 引导宿主授权。 |
| **统一安全/恢复评估 (Canonical Safety/Recovery)** | `SafetyRecoveryEvaluation` | **已完成 (Verified)** | 计划生成 (`get_training_plan`)、处方生成 (`_evaluate_training_prescription`)、渐进建议 (`_evaluate_exercise_progression`) 与确认 (`confirm_training_progression`) 统一复用同一纯评估函数；精准解包嵌套 `metrics` 并保持 0 数值；受限模式、7天减载、急性疲劳/睡眠亏损/低恢复分及伤病禁忌集中判定；严格依用户本地时区与 `day <= target_date` 过滤防未来记录遮蔽。 |
| **双层记忆查询 (Dual-Tier Query)** | `query_memory` | **已完成 (Verified)** | 第一层实时查询 SQLite 短期事实；第二层通过 `MemoryProvider` 接口调用长期记忆；严格保留来源 confirmation_status（未提供默认 `unconfirmed`，禁止自动升级为 `confirmed_wiki`）；置信度缺省为 `None`（杜绝虚构 `0.9`）；严格受限于 `limit`；畸形载荷安全降级。 |
| **本地 TTL 趋势聚合** | `maintain_memory` | **已完成 (Verified)** | 确定 ISO 周窗口、用户时区归日；每日摄入汇总后再求日均（拒绝全餐食混淆平均，不将缺失日虚构为 0，完整披露 `window_days`, `recorded_days`, `missing_days`）；迟到餐食纠错触发 `parent_id` 修订链替换旧走势；安全清理明细（`foods_json='[]'`）并完整保留宏量因果链。 |
| **便携式数据迁移安全** | 完整安全档案、修订链导出与严格导入校验 | **已完成 (Verified)** | 禁止未知 schema；严格 JSON 嵌套与时区校验；过时备份禁止回退安全模式；持久字段规范化全量冲突检测；事务整体原子回滚。 |

### 1.2 外部依赖真实集成状态与待办披露（严禁宣称生产部署已完成）

> [!WARNING]
> **真实集成状态客观声明**：本地 Core 与 MCP 服务端在当前规格范围内测试完成；严禁宣称“产品生产部署已完成”或“真实集成已验证”。以下为物理生产环境中待接入的真实外部系统与宿主依赖：

1. **MemoryProvider（Obsidian Vault 真实集成）**：
   - **真实状态：未连接**。
   - **降级机制**：Cyber Health 坚守安全隔离红线，**绝不直接读写或篡改用户本地 Obsidian Vault 物理文件**。在未注入有效外部 `MemoryProvider` 时，所有候选记忆自动入库 `memory_outbox` 表排队，返回 `partial` 状态与 `MEMORY_DEFERRED` 警告。
   - **严正声明**：**不宣称已连接用户 Obsidian Vault，未经过真实物理笔记写入**。
2. **宿主主动定时推屏与定时器**：
   - **真实状态：未注册**。
   - **降级机制**：Core 为短事务无状态 MCP 服务，输出带精确触发条件、抑制原因及墓碑标识的动态日程快照。实际系统弹窗与推送依赖宿主调度守护（Cron/Timer）。
3. **临床处方与执业医师审阅**：
   - **真实状态：算法规则与权威指南已内置，执业医师外部审阅待接入**。
   - 系统输出携带 `evidence_rules_algorithmic_pending_licensed_physician_review` 标识与 `NON_DIAGNOSTIC` 法律免责声明；红旗症状强制阻断系统建议并指导线下紧急就医。
4. **多模态视觉解析与智能穿戴硬件直连**：
   - **真实状态：由宿主提供**。
   - Core 接收结构化食物明细与体征数值，不内建重型机器视觉模型或蓝牙同步 Sidecar。

---

## 2. 历次审核阻断项与契约修复对照

### 2.1 第六轮审核阻断项与契约修复（导入校验与全字段冲突）
依据 `docs/codex-review-round6.md` 与 `tests/test_codex_import_validation.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **6.1** | **未知 schema 导入被接受**：备份传入未知 `0.999.999` schema 成功导入。 | 建立严格 schema 白名单表 `SUPPORTED_SCHEMA_VERSIONS = {"0.1.0", "0.2.0", "0.2.1"}`，超出白名单直接抛出 `ValidationError`。 | `test_codex_import_validation.py::test_unsupported_schema_version_rejected` (PASS) |
| **6.2** | **损坏 goals_json 破坏后续查询**：损坏的 JSON 字符串被导入库中，导致后续 `get_profile` 反序列化崩溃。 | 导入层对 `goals_json`, `constraints_json`, `safety_flags_json` 执行严格 JSON 解码与嵌套类型校验；并在事务中执行，失败原子回滚。`get_profile` 使用确定性读取操作 ID 避免无意义版本漂移。 | `test_codex_import_validation.py::test_malformed_profile_json_rejected_atomically` (PASS) |
| **6.3** | **不存在的时区被接受**：`Atlantis/Unknown` 假时区未被校验入库。 | 调用 `ZoneInfo(tz_name)` 校验 IANA 有效性，非法或不存在的时区抛出 `ValidationError` 并回滚。 | `test_codex_import_validation.py::test_nonexistent_timezone_rejected` (PASS) |
| **6.4** | **非法 safety_mode 被接受**：`hacked_mode` 假模式成功入库。 | 校验 `safety_mode in ("normal", "restricted")`；且禁止旧备份在活跃受限状态下清零安全模式，违规直接抛 `ConflictError`。 | `test_codex_import_validation.py::test_invalid_safety_mode_rejected` (PASS) |
| **6.5** | **全量比对遗漏 causation_id / state_version 与审计载荷**。 | 补齐全字段规范化比对：餐食增加 `causation_id`, `state_version`；领域记录覆盖 `body`, `status`, `parent_id`；日程覆盖窗口与 revision；操作日志覆盖 `request_hash`, `payload_json`, `before_version`, `after_version`。 | `test_codex_import_safety.py`, `test_codex_import_validation.py` (PASS) |

### 2.2 第七轮审核阻断项与契约修复（记忆证据保留、趋势聚合与操作安全）
依据 `docs/codex-review-round7.md` 与 `tests/test_codex_memory_evidence.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **7.1** | **query_memory 虚构 confirmed_wiki 与 0.9 置信度，未限制返回条数，畸形响应残留**。 | 严格保留来源 confirmation_status（未提供默认为 `unconfirmed`，杜绝自动升级为 `confirmed_wiki`）；置信度缺失默认为 `None`（杜绝虚构 `0.9`）；严格受限于 `limit` 截断；对非 dict/非 list 畸形响应安全降级并输出明确 warnings，异常时彻底清空 `obsidian_memories` 避免残存孤儿状态。 | `test_codex_memory_evidence.py` (2 项全 PASS), `test_domain_memory_and_trends.py` (PASS) |
| **7.2** | **maintain_memory 历史餐食混淆平均、覆盖无版本链、未清理食材明细**。 | 严格落实原规格第 6 节与 Table 8：先按用户时区按日汇总摄入，再按记录天数求日均（不制造缺失日为 0，完整披露 `window_days`, `recorded_days`, `missing_days`）；发生纠错或迟到记录时，通过 `parent_id` 修订链替换旧走势；将压缩老餐食的 `foods_json` 清理为 `'[]'`，同时保留数值指标与因果审计链。 | `test_domain_memory_and_trends.py` (多餐日对比多日单餐、缺失天数披露、幂等执行、迟到纠错修订链、明细安全清理共 5 项新测试全 PASS) |
| **7.3** | **memory_action 任意字符串转发且 destructiveHint=False**。 | 严格限制规格枚举 `VALID_MEMORY_ACTIONS`；在进入任何数据库事务或外部 IO 前以 `MemoryActionInput` 验证操作载荷（`confirm`/`delete` 强制要求非空目标与 `confirmed=True`，否则抛出 `ValidationError`）；MCP 声明细粒度 `destructiveHint=True`。 | `test_domain_memory_and_trends.py::test_memory_action_validation_blocks_unconfirmed_delete_and_invalid_action` (PASS), `test_mcp_stdio.py` (PASS) |

### 2.3 第八轮审核阻断项与契约修复（计划与安全规则一致性、决策矩阵与末餐清理）
依据 `docs/codex-review-round8.md` 与 `tests/test_codex_unconfigured_plan.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **8.1** | **未配置目标凭空默认 1800-2100kcal 与 120g 蛋白质**。`daily_review` 与 `plan_tomorrow` 硬编码未配置默认值并允许静默 commit。 | 统一实现 `_resolve_nutrition_targets`：未配热量一律返回 `None` 并请求配置；仅配热量时蛋白明确保持 `None`（`status="calories_only"`）；`get_today`, `daily_review`, `plan_tomorrow` 彻底废除 `unconfigured_default`；commit 未配置计划输出明确提示警告。 | `test_codex_unconfigured_plan.py` (2 项全 PASS) |
| **8.2** | **训练入口忽略伤病约束与器械限制，无体征记录虚构“状态优良”**。`_evaluate_training_prescription` 查询 constraints 却不使用，无近期状态仍输出标准渐进。 | 全面重构为统一决策矩阵：严格解析 `constraints_json`（膝伤、肩伤、腰伤禁忌动作替换，如深蹲换臀桥/后链，推胸换划船/支撑）；适配器械（杠铃/哑铃/自重）；解析训练经验级别；无近期体征记录时明确披露 `unrecorded_recent_state` 并指导录入，严禁输出“状态优良”；注入法定非医疗处方免责声明。 | `test_domain_memory_and_trends.py::test_training_prescription_unified_decision_matrix` (PASS), `test_domain_remaining.py::test_get_training_plan_states` (PASS) |
| **8.3** | **周聚合维护删除末餐留下过时活跃趋势**。维护后用户删除某周最后一餐，原活跃趋势被遗留为孤儿 active 状态。 | `maintain_memory` 增强孤儿活跃趋势扫描：对历史范围内在 `weeks_map` 中无任何有效餐食的活跃趋势，更新为 `status='superseded'`，并插入撤回记录（`status='superseded'`，通过 `parent_id` 继承旧 ID 形成完整纠错因果链）；重复维护幂等（0 次冗余写入）；TTL 清理时受 parent_id 引用保护不被破坏。 | `test_domain_memory_and_trends.py::test_maintain_memory_supersedes_stale_trend_when_all_meals_deleted` (PASS) |

### 2.4 第九轮审核阻断项与契约修复（核心训练闭环与双重渐进状态机）
依据 `docs/codex-review-round9.md` 与 `tests/test_codex_review_round9.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **9.1** | **训练处方缺少核心渐进提议与确认闭环**。仅有降载状态机，无连续达标加重逻辑与版本修订链。 | 实现 `TRAIN_PROGRESS_01` 双重渐进状态机：要求连续 2 次达到目标次数且 RPE<=8 提议加重；显式关联证据 ID；`confirm_training_progression` 带 `parent_id` 修订链原子确认。 | `test_codex_review_round9.py` (9 项全 PASS) |
| **9.2** | **多约束动作过滤存在组合漏洞与安全隐患**。多关节禁忌（膝+腰、膝+肩）发生单层覆盖或处方冲突动作。 | 建立 `EXERCISE_CATALOG` 属性库，采用多约束交集过滤；候选动作均冲突时触发 `TRAIN_CONSTRAINTS_SUSPENDED` 并提供专业转诊指引。 | `test_codex_review_round9.py::test_combined_constraints_resolution` (PASS) |
| **9.3** | **陈旧体征用于今日处方伪造健康分**。历史久远记录未失效，掩盖今日真实疲劳风险。 | 引入 `evidence_window_days` 有效窗口（默认 1 天）；超期数据按未知处理，披露 `unrecorded_recent_state` 且 `recovery_score` 置为 None。 | `test_codex_review_round9.py::test_stale_daily_state_evidence_rejected` (PASS) |
| **9.4** | **临场器械受限缺少同动模式替换**。器械被占或局部不适无法安全调整动作。 | 实现 `substitute_exercise`：依据运动模式与器械过滤候选，输出同模式等效动作及结构化组间间歇。 | `test_codex_review_round9.py::test_movement_pattern_substitution` (PASS) |

### 2.5 第十轮审核阻断项与契约修复（渐进建议与确认共享状态机、证据校验与基线解耦）
依据 `docs/codex-review-round10.md` 与 `tests/test_codex_review_round10.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **10.1** | **_evaluate_exercise_progression 跳过最近失败训练误提议加重**。遇完成率<1 直接 continue 回溯更早成功；同日多次记录未去重。 | 重构为严格选同动作最近两会话并判定连续达标；最近一次失败（完成率<1、RPE>8、次数不足或组数不足）直接打断 streak（返回 None）；同日拆分记录按 `(day, session_id)` 规范归并。 | `test_codex_review_round10.py::test_recent_failed_session_breaks_streak_instead_of_skipping` (PASS), `test_same_day_split_sessions_consolidated_correctly` (PASS) |
| **10.2** | **confirm_training_progression 只校验 restricted，缺少全量安全重算**。活跃 Deload、疲劳/睡眠亏损、关节禁忌被静默绕过。 | 确认接口在事务内共享安全状态机重算：受限模式、活跃 7 天减载、急性疲劳/睡眠亏损（`TRAIN_RECOVERY_01`）、关节伤病禁忌集中阻断，抛出对应 `SafetyInterventionError` / `ConflictError`。 | `test_codex_review_round10.py::test_confirm_progression_safety_gates_rechecked_in_transaction` (PASS) |
| **10.3** | **缺乏待确认建议 ID 验证与证据来源防伪**。空或假 source_record_ids 可绕过，任意负荷可确认；跨用户污染。 | 生成确定性 `proposal_id`（签名摘要，绑定用户、动作、有序证据 ID、目标增量）；确认时严格校验 `proposal_id` 与 `source_record_ids` 存在性、激活状态、归属用户及一致性；负荷错配/跨用户坚决拒绝；支持幂等重放。 | `test_codex_review_round10.py::test_confirm_progression_evidence_verification_and_tamper_rejection` (PASS) |
| **10.4** | **组数/负荷可比性缺失与缺项默认成功**。任意一组达标即算成功，缺少组数与真实 RPE 未如实披露。 | 要求达到动作默认正式组数（`default_sets`）；两次会话必须同负荷且达到目标上限次数；缺失组数/完成度/RPE 坚决不默认成功；如实披露全部判定阈值（`required_consecutive_sessions: 2`, `required_completion_rate: 1.0`, `max_allowed_rpe: 8.0` 等）。 | `test_codex_review_round10.py::test_multiset_and_load_comparability_enforced` (PASS), `test_missing_sets_or_rpe_never_defaults_to_success` (PASS) |
| **10.5** | **自重动作加次确认与手工基线负荷解耦**。确认接口仅支持重量；手工记录基线负荷混入“达标建议确认”。 | 模型与 MCP 增加 `confirmed_reps` 支持自重动作渐进确认；新增 `record_exercise_baseline` 独立接口记录当前基线负荷，避免伪造“达标建议”证据链。 | `test_codex_review_round10.py::test_bodyweight_rep_progression_confirmation` (PASS), `test_manual_baseline_separated_from_progression_confirmation` (PASS) |
| **10.6** | **真实 Core 及 stdio JSON-RPC 边界调用验证**。缺乏跨进程 stdio 端到端调用渐进确认的回归覆盖。 | 编写真实 MCP stdio JSON-RPC 跨进程测试，覆盖完整初始化协议、`cyber_health_confirm_training_progression` 握手与错误信封。 | `test_codex_review_round10.py::test_mcp_stdio_confirm_progression_boundary` (PASS) |

### 2.6 第十一轮审核阻断项与契约修复（真正共享安全评估纯函数与指标保真）
依据 `docs/codex-review-round11.md` 与 `tests/test_codex_progression_fatigue.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **11.1** | **confirm_training_progression 重复实现安全逻辑并读取顶层 metrics**。`log_daily_metrics` 指标保存在 `body_json["metrics"]`，确认读取顶层字段全部返回 None。 | 抽取统一纯评估函数 `_evaluate_user_safety_and_recovery`，统一从 `body["metrics"]` 读取指标并回退兼容顶层；杜绝复制第三套 if。 | `test_codex_progression_fatigue.py::test_sleep_deficit_blocks_previously_generated_proposal` (PASS) |
| **11.2** | **阈值不一致与 falsy 判定把 0 误当健康**。确认阈值写成 fatigue>=8 / recovery<40（与计划入口 >=7 / <60 矛盾）；`or 8.0` / `or 100.0` 把 0.0 误当未知并默认健康。 | 统一阈值判定：`sleep_hours < 6.0`、`fatigue_level >= 7.0`、`recovery_score < 60`；显式检查 `is not None`，严格保留 `0.0` 与 `0` 真实输入并阻断渐进。 | `test_codex_progression_fatigue.py::test_fatigue_level_seven_blocks_progression_via_real_api` (PASS), `test_sleep_hours_zero_not_falsy_blocks_progression` (PASS), `test_recovery_score_zero_not_falsy_blocks_progression` (PASS) |
| **11.3** | **未来记录按日期倒排遮蔽当前活跃疲劳**。`ORDER BY day DESC LIMIT 1` 无 `<= target_date`，未来记录造成 `days_diff < 0` 从而漏判今日疲劳。 | `_evaluate_user_safety_and_recovery` 严格按 `day <= target_date` 进行时间围栏过滤；`_evaluate_exercise_progression` 确认时传入 `date=target_date` 过滤未来训练。 | `test_codex_progression_fatigue.py::test_future_daily_record_does_not_shadow_current_fatigue` (PASS), `test_future_workout_does_not_count_towards_progression` (PASS) |
| **11.4** | **用户本地时区确定目标日与过期体征自然失效**。时区未对齐导致日期错位；超期体征自然失效。 | 基于用户档案 `timezone` 精确计算用户目标自然日；超过 `recovery_evidence_window_days` 的过期疲劳自然失效并放行正常渐进。 | `test_codex_progression_fatigue.py::test_stale_fatigue_data_expires_and_allows_progression` (PASS), `test_timezone_aware_determines_local_target_date` (PASS) |

### 2.7 第十二轮审核阻断项与契约修复（动态日程同步、墓碑快照、事实驱动抑制与只读纯度）
依据 `docs/codex-review-round12.md` 与 `tests/test_codex_schedule_sync.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **12.1** | **5 个标准窗口与稳定事件标识**。原日程仅 4 个窗口缺失晚餐（19:30）；午晚餐标识冲突。 | 标准窗口补齐至 5 个（晨间唤醒 07:30、午餐核验 13:00、训练窗口 17:30、晚餐核验 19:30、晚间对账 21:30）；生成确定性稳定 ID（区分 `meal_check_lunch` 与 `meal_check_dinner`）。 | `test_codex_schedule_sync.py::test_five_standard_windows_generated_with_stable_ids` (PASS) |
| **12.2** | **计划推迟与状态修改防覆盖保护**。重新调用日程生成会强制将已推迟（`revision > 1`）的窗口覆盖回默认值。 | 在 `schedule_daily_reminders` 中检测已有记录：若 `revision > 1` 或 `status != 'pending'`，坚决保留现有窗口、状态与版本号，杜绝覆盖用户延期。 | `test_codex_schedule_sync.py::test_postponement_preserved_on_rescheduling` (PASS) |
| **12.3** | **全量快照与墓碑支持**。`get_schedule` 仅返回 pending/overdue，宿主无法感知取消/跳过导致旧定时器遗留。 | 增加 `include_inactive: bool = False` 参数；当为 True 时返回包含 `cancelled` / `skipped` / `acknowledged` / `delivered` 等墓碑事件的全量快照，供宿主安全撤销已排程定时器。 | `test_codex_schedule_sync.py::test_tombstone_snapshot_for_host_timer_cancellation` (PASS) |
| **12.4** | **只读纯度与版本审计契约**。原 `get_schedule` 在读操作中执行未审计的 SQL UPDATE 将状态置为 overdue。 | `get_schedule` 改为纯派生只读连接（`store.connect()`），在内存中纯派生 `overdue` 状态与 `compensation_required` 标记；零 SQL 写操作，不篡改 `state_version` 与 `operation_log`，与 `readOnlyHint=True` 完全一致。 | `test_codex_schedule_sync.py::test_read_purity_of_get_schedule` (PASS) |
| **12.5** | **事实驱动的动态触发与抑制原因重算**。日程缺乏 `trigger_condition`、`eligible` 与结构化 `suppression_reason`。 | `get_schedule` 实时重算当前事实：午餐/晚餐已记（`lunch_already_logged`/`dinner_already_logged`）、训练已完成/休息日/受限模式（`workout_already_completed`/`scheduled_rest_day`/`safety_restricted_mode`）、晨间计划已锁（`morning_plan_already_committed`）、档案禁用提醒（`reminders_disabled_by_user`）及墓碑状态均精准抑制并披露原因。 | `test_codex_schedule_sync.py::test_dynamic_eligibility_suppression_rules`, `test_profile_reminders_disabled_suppresses_all`, `test_restricted_mode_suppresses_workout_reminder` (PASS) |
| **12.6** | **MCP stdio 端到端跨进程同步验证**。缺乏真实 stdio 协议下的拉取→延期→重拉→取消→墓碑撤销生命周期测试。 | 编写跨进程 stdio JSON-RPC 异步测试，完整模拟宿主客户端生命周期调度交互。 | `test_codex_schedule_sync.py::test_mcp_stdio_schedule_sync_lifecycle` (PASS) |

### 2.8 第十三轮审核阻断项与契约修复（日程完成证据真伪、草稿计划锁定与记忆维护宿主闭环）
依据 `docs/codex-review-round13.md` 与 `tests/test_codex_review_round13.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **13.1** | **部分完成训练被日程误当完全完成并永久抑制提醒**。`get_schedule` 只要当日存在 workout/workout_log 记录即判定 `workout_already_completed`，完成率 0.2 或未记录完成率被错误冒充为已完成。 | `get_schedule` 深入解析 workout 记录正文中的 `completion_rate`：要求 `completion_rate is not None and float(completion_rate) >= 1.0` 才标记 `workout_already_completed`；部分完成（`0.0 < completion_rate < 1.0`）与未提供完成率（`completion_rate is None`）保持 `eligible = True`，绝不冒充完成。 | `test_codex_review_round13.py::test_partial_workout_does_not_suppress_schedule_reminder`, `test_missing_completion_rate_does_not_suppress_schedule_reminder`, `test_full_workout_suppresses_schedule_reminder` (PASS) |
| **13.2** | **晨间计划检查混淆已废止的旧 commit**。计划检查使用 `status != 'deleted'`，当历史 commit 被后续 draft 取代为 `superseded` 时，仍被当作已锁定的 commit 误抑制提醒。 | `get_schedule` 查询计划时严格排除废止记录：`status NOT IN ('superseded', 'deleted')`；当最新非废止记录为 draft 时，晨间提醒保持 `eligible = True`。 | `test_codex_review_round13.py::test_superseded_plan_commit_ignored_when_latest_draft_exists` (PASS) |
| **13.3** | **单一读事务快照隔离**。`get_schedule` 包含多次 SELECT 查询，需在原子读快照下执行。 | `get_schedule` 封装于单一 `conn.execute("BEGIN") ... finally: conn.execute("COMMIT")` 读快照事务中；严格执行 0 次写操作，行数、版本与审计日志无任何变更。 | `test_codex_review_round13.py::test_get_schedule_pure_read_snapshot_isolation` (PASS) |
| **13.4** | **Spec 6.1 随路/惰性记忆维护闭环（纯只读提示 + 宿主显式维护契约）**。严禁将 G-5 推至 P1；严禁在只读接口中隐式写库或启动无界后台线程。 | 架构明确划分三个层次：<br>1. **Core 提示层**：`daily_review` 原子排队候选意图至 `memory_outbox`，返回结构化 `maintenance_recommended: True`、`suggested_action: "cyber_health_maintain_memory"` 及稳定维护键 `maint_{user_id}_{date}`；`get_today` 在纯只读快照下检查 pending outbox，按需输出维护提示，严格 0 写库。<br>2. **模拟宿主执行层**：宿主收到提示后显式调用 `cyber_health_maintain_memory` 排空待办意图，成功后后续读取自动清除提示；重复触发具有幂等性保障。<br>3. **真实宿主未启用态**：默认未连接 MemoryProvider 时，维护操作安全降级返回 `partial` 与 `MEMORY_DEFERRED` 警告，意图保留为 pending 并记录重试次数，事实读写完全不受阻断。 | `test_codex_review_round13.py::test_daily_review_queues_memory_intent_and_hints_maintenance`, `test_scoped_maintenance_host_drain_and_idempotency`, `test_scoped_maintenance_provider_failure_non_blocking`, `test_stdio_mcp_scoped_maintenance_simulation`, `test_stdio_mcp_unenabled_host_non_blocking_simulation` (全 PASS) |

### 2.9 第十四轮审核阻断项与契约修复（维护续跑状态机、代次键与复盘/规则解耦）
依据 `docs/codex-review-round14.md` 与 `tests/test_codex_review_round14.py`：

| 编号 | 缺陷与规格要求 | 修复措施 | 对应测试验证 |
| :--- | :--- | :--- | :--- |
| **14.1** | **`get_today` 仅统计 status=pending 忽略 TTL 到期餐食与过期租约**：没有 outbox 但有过期事实时永远不提示维护。 | 实现统一纯读 `_calculate_maintenance_due` 计算器，全面覆盖 pending outbox、过期 `in_flight` 租约、到期餐食明细剪枝（`foods_json != '[]'`）、到期未引用 superseded 记录与已发送 outbox 清理，并在结构化输出中准确披露原因。 | `test_codex_review_round14.py::test_unified_due_work_calculator_detects_ttl_meal_details_and_discloses_reasons`, `test_unified_due_work_calculator_detects_expired_in_flight_leases` (PASS) |
| **14.2** | **固定 maintenance_key 导致重放旧响应阻断续跑**：`maint_{user_id}_{day}` 导致重试、第二批（>50）或同日新任务命中幂等缓存。 | 设计基于工作代次（批次 50 项的 `intent_id:attempts:status` 及 TTL 计数）的稳定哈希键 `maint_{user_id}_{day}_g{hash}` 与续跑令牌。重复请求安全重放旧缓存；批次推进、失败重试（`attempts` 递增）或新任务注入自动生成新代次键；返回 `has_more`, `next_maintenance_key`, `continuation_token` 与退避时间 `retry_after_seconds`，禁止紧循环重试。 | `test_codex_review_round14.py::test_fifty_one_plus_tasks_continuation_advances_without_idempotency_block`, `test_partial_failure_followed_by_provider_recovery`, `test_repeated_get_today_idempotent_key_stability`, `test_same_day_new_task_generates_fresh_key` (PASS) |
| **14.3** | **`daily_review` 直接向 outbox 写 propose 冒充健康规则**：绕过统一 `propose_memory_candidate` 且与 `memory.propose` 混用，每次复盘均自动把全文推给 Obsidian。 | 坚决删除 `daily_review` 向 `memory_outbox` 的直接写操作；复盘仅维护本地 `domain_record`（复盘与计划草案）；维护调度通过 `_calculate_maintenance_due` 检测真实待办；长期健康规律统一通过 `propose_memory_candidate` 并采用规范协议 `memory.propose` 提交。 | `test_codex_review_round14.py::test_daily_review_does_not_spam_rule_proposals_or_bypass_propose_candidate` (PASS) |

---

## 3. 真实宿主隔离探针结果记录

执行命令：
```bash
.venv/bin/python tests/probe_openclaw.py
```

执行环境：macOS (Staff), OpenClaw CLI 2026.8.2，沙箱临时隔离目录。  
执行结果输出：
```json
{
  "path": "/var/folders/qd/v8qvpxhs7vnd1vgxrm1g9kx40000gn/T/cyber-health-host-probe-xxxx/openclaw.json",
  "ok": true,
  "servers": [
    {
      "name": "cyber-health",
      "ok": true,
      "issues": []
    }
  ]
}
```

> [!NOTE]
> **探针结果对比**：
> - 在补充细粒度 `ToolAnnotations`（只读、破坏性、幂等与外部世界属性）之后，OpenClaw 宿主探针报告 **`ok: true` 且 `issues: []`（0 警告）**。原 `tools have no safety annotations` 提示已彻底消除。
> - 探针在完全独立的临时目录中运行，**未修改用户正式 OpenClaw 配置、未触碰真实健康数据、未写入 Obsidian Vault**。
> - 探针证实：OpenClaw CLI 可成功添加该 MCP 服务并通过严格的宿主安全发现检验。

---

## 4. 全量自动化测试与构建发布验证（130/130 全绿）

> [!NOTE]
> **测试套件构成客观声明**：全量 130 项自动化测试由 **89 项 Codex 审查与独立验收驱动用例** 与 **41 项 Gemini 领域契约与回归用例** 共同构成。不虚称全部规格需求均由独立审核员逐条覆盖，而是明确披露双来源测试体系：Codex 独立测试重点压测并发冲突、状态机流转、安全性拦截、边界攻击与协议契约；Gemini 领域测试重点覆盖领域事实持久化、跨会话乐观锁、业务规则聚合与 stdio 跨进程通信。

### 4.1 自动化测试执行结果
执行环境：`.venv`（Python 3.12.13, mcp 1.29.1, pydantic 2.13.5）  
执行命令：
```bash
.venv/bin/python -m unittest discover -s tests -v
```

执行结果：
```text
Ran 130 tests in 4.158s

OK
```

### 4.2 用例分类明细（共 21 个测试文件，130 项测试）

#### Part A. Codex 审查与对抗验收用例（14 轮共 89 项，全部 PASS）
1. `tests/test_codex_review.py` (8/8)：第一轮基础契约（必填幂等键、真实日历校验、食物区间合法性、当地昨日复用、红旗持久化、受限模式计划安全、日程即时比对、修订版本强制）。
2. `tests/test_codex_review_round2.py` (7/7)：第二轮（控制台入口、Outbox 多用户隔离、IO 前原子预留、档案红旗受限模式、日程稳定 ID、时区夏令时偏移、只读事务快照）。
3. `tests/test_codex_outbox_concurrency.py` (3/3)：第三轮（分隔符哈希防碰撞、IO 前短事务锁定、租期防抢占）。
4. `tests/test_codex_review_round4.py` (4/4)：第四轮（档案受限防静默清除、全载荷规范化哈希、非抢占租期重入、睡眠<6h 统一减载）。
5. `tests/test_codex_import_safety.py` (2/2)：第五轮（旧备份禁止清除受限模式、全持久字段规范化比对）。
6. `tests/test_codex_import_validation.py` (4/4)：第六轮（未知 schema 拒绝、损坏 JSON 事务回滚、假时区拒绝、非法 safety_mode 拒绝）。
7. `tests/test_codex_memory_evidence.py` (2/2)：第七轮（长期记忆置信度与状态保真、严格 limit 截断与畸形降级）。
8. `tests/test_codex_unconfigured_plan.py` (2/2)：第八轮（未配置目标真实披露为 None、拒绝默认虚构 1800kcal）。
9. `tests/test_codex_review_round9.py` (9/9)：第九轮（双重渐进状态机、多约束动作交集过滤、陈旧体征失效、同动模式替换）。
10. `tests/test_codex_review_round10.py` (14/14)：第十轮（渐进建议与确认共享状态机、最近失败断链、同日合并、签名摘要防伪、自重加次确认、基线解耦、MCP 边界）。
11. `tests/test_codex_progression_fatigue.py` (8/8)：第十一轮（纯安全评估函数统一复用、真实 API 指标提纯与 0 值保真、未来记录防遮蔽、过期自然失效）。
12. `tests/test_codex_schedule_sync.py` (8/8)：第十二轮（5个标准时间窗口、延期防覆盖、全量快照与墓碑导出、只读纯度与无写库、事实驱动抑制、跨进程 stdio 同步）。
13. `tests/test_codex_review_round13.py` (10/10)：第十三轮（训练部分完成 0.2 不抑制、无完成率不冒充、完全完成抑制、旧 commit 排除、只读事务快照、随路维护 Core 纯读提示 + 宿主显式维护契约、stdio MCP 模拟与未启用态）。
14. `tests/test_codex_review_round14.py` (8/8)：第十四轮（TTL 到期餐食明细识别与原因披露、崩溃 Worker 租期恢复、重复 `get_today` 键稳定性、51+ 任务续跑无阻断、Provider 故障退避与恢复重试、同日新任务代次刷新、复盘规则解耦、stdio MCP 续跑闭环）。

#### Part B. Gemini 领域契约与系统回归用例（7 个模块共 41 项，全部 PASS）
15. `tests/test_p0_contracts.py` (6/6)：P0 核心契约（只读纯度、5 会话多实例交互、幂等匹配 vs 错配、乐观版本冲突、时区归并、区间非法校验）。
16. `tests/test_domain_advanced.py` (6/6)：高级领域流（餐食软删除与复用、体征恢复评分、红旗锁定与 7 天 Deload 解除协议、复盘计划锁定、日程生命周期、MemoryProvider 降级与锁外重试）。
17. `tests/test_cross_session.py` (4/4)：跨会话与跨实例事实一致性与乐观并发版本锁。
18. `tests/test_mcp_stdio.py` (2/2)：MCP stdio 跨进程客户端验证（默认模式严格 6 工具，`--allow-all` 严格 26 工具，ToolAnnotations 安全注解，0 运行时告警）。
19. `tests/test_outbox_concurrency_extended.py` (4/4)：Outbox 扩展状态机（安全重放、50条分批截断、崩溃 Worker 租期恢复、TTL 清理与父引用保留）。
20. `tests/test_domain_remaining.py` (6/6)：自适应运动处方、红旗阻断打卡、真实一手文献知识库、数据迁移往返、日程逾期补偿、MCP 错误脱敏。
21. `tests/test_domain_memory_and_trends.py` (13/13)：双层记忆查询、未配置/配置下剩余热量指导、本地周均趋势聚合、缺失天数披露、幂等维护、迟到纠错修订链、明细安全清理、记忆操作事前校验、删除末餐过时趋势废止与因果链闭环、训练全维度决策矩阵。

---

### 4.3 语法编译、锁检查与软件包构建验证

1. **Python 字节码静态编译 (`compileall`)**：
   ```bash
   .venv/bin/python -m compileall cyber_health cyber_health_mcp tests
   ```
   结果：100% 编译通过，无任何语法错误或弃用警告。
2. **依赖锁文件一致性 (`uv lock --check`)**：
   ```bash
   uv lock --check
   ```
   结果：Resolved 31 packages in 16ms，锁文件与 `pyproject.toml` 100% 同步。
3. **分发包构建 (`uv build`)**：
   ```bash
   uv build
   ```
   结果：
   - 成功生成源码包：`dist/cyber_health_agent-0.2.1.tar.gz`
   - 成功生成 Wheel 包：`dist/cyber_health_agent-0.2.1-py3-none-any.whl`
   - 版本严格标定为 **0.2.1**，无任何版本漂移。


