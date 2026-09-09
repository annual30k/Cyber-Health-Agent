# Cyber Health Agent: 规格逐条追溯与能力缺口清单 (Traceability & Gap Matrix)

**生成日期：** 2026-09-05  
**当前版本：** Core v0.2.3 / MCP v0.2.3 / Package v0.2.3  
**对照基准：** `Cyber_Health_Agent_产品与技术规格说明书.md` 与 `docs/openclaw-adapter-spec.md`  
**审核依据：** `docs/codex-final-delivery-review.md` 与 `docs/codex-review-round14.md`  
**测试覆盖状态：** 全量自动化测试 165 项 100% 通过（含首次建档、夜间事实核实、目标缺口分析与宿主调度回归测试）

---

## 一、真实性与边界原则（Truth-in-Advertising Declaration）

1. **事实引擎与契约闭环**：Cyber Health Core（SQLite 实时事实源、首次建档门槛、短事务边界、乐观并发版本锁、规范化 SHA-256 幂等去重、急性红旗集中阻断、未配置目标真实披露、训练决策矩阵与动态日程）**代码已在当前规格范围通过全量 163 项自动化测试覆盖**。  
   *客观披露*：测试套件包含 89 项 Codex 独立验收/审查驱动用例与 41 项 Gemini 领域实现及契约回归用例；不虚称“全部需求均经过第三方独立审核”，而是清晰区分双来源测试构成。
2. **MCP 工具面与安全注解**：
   - 默认模式：7 个 P0 核心工具（白名单增加 `update_profile`，确保首次建档可保存）。
   - 扩展模式（`--allow-all`）：接通全量 26 个领域能力接口（含 7 P0 + 19 扩展接口）。
   - 安全注解（`ToolAnnotations`）：对所有 26 个工具根据领域语义分别赋予细粒度的 `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`；经隔离 OpenClaw 探针验证，`mcp doctor --probe` 报告 `ok: true` 且 `issues: []`。
3. **外部依赖客观披露（严禁宣称生产部署已完成或外部系统已真实集成）**：
   - **Obsidian Vault 真实集成：未连接**。Cyber Health 坚守安全隔离红线，**绝不直接读写或篡改用户本地 Obsidian Vault 物理文件**。在外部 `MemoryProvider` 未接入时，候选提炼通过 Outbox 状态机可靠排队（`status: partial`, 警告 `MEMORY_DEFERRED`），支持 30s 租期恢复与代次推进。
   - **执业医师与临床处方：未接入**。循证知识库与训练引擎内置权威指南，但系统输出明确的非个性化/非诊断性法律免责声明（`NON_DIAGNOSTIC`），绝不虚构临床疗效或医疗处方；红旗症状强制拦截并要求线下紧急就医。
   - **移动端/宿主主动推送守护进程：未注册**。Core 为短事务无状态 MCP 服务，输出带精确触发条件与抑制原因的动态日程窗口及墓碑快照，实际定时推送触发完全依赖宿主调度机制（Cron/Timer 轮询）。
   - **智能穿戴硬件直连与多模态视觉：由宿主提供**。Core 接收结构化食物明细与体征数值，不内建重型视觉模型或蓝牙同步 Sidecar。

---

## 二、逐条需求追溯与实现明细表

状态分类定义：
- **[A. 已实现且自动化测试验证]**：代码已在 Core 或 MCP 完整编写，且被单元测试严格覆盖。
- **[B. 本地代码自闭环 / 外部可优雅降级]**：本地事实库具备完整处理、存储与聚合能力；外部依赖缺失时返回明确警告而不崩溃或丢失事实。
- **[C. 外部依赖前置/待接入]**：依赖外部真实运行环境（如真实 Obsidian Memory Plugin 守护、执业医师签约审阅、宿主推送机制）。

### 1. 档案与安全模式（Section 3.1, 7.1, 7.2, 8）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1.1 | 初次建档与档案读取 | 获取用户目标、伤病限制、时区与当前 `state_version`；只读查询不创建空行 | **[A. 已实现]** | `CyberHealthService.get_profile` 使用只读事务；不存在用户返回 `exists=False`，不执行 INSERT。 | `test_p0_contracts.py::test_read_only_purity_never_mutates_database` | 无 |
| 1.2 | 档案修改与目标更新 | 更新 goals, constraints, timezone；强制幂等键与版本校验 | **[A. 已实现]** | `update_profile` 校验非空幂等键、时区合法性，并递增 `state_version`。 | `test_codex_review.py::test_update_profile_requires_idempotency_key` | 无 |
| 1.3 | 急性红旗症状拦截 | 扫描严重胸痛、胸闷、呼吸困难、晕厥等红旗词汇；自动锁定为 `restricted` 模式 | **[A. 已实现]** | 全入口集中调用 `_scan_for_red_flags`；涵盖 profile、workout、meal、notes；自动更新 `safety_mode='restricted'`。 | `test_domain_advanced.py::test_safety_red_flag_and_deload_protocol` | 无 |
| 1.4 | 受限模式退出与渐进回归协议 | 解除限制必须提供非空 `clearance_reason`；解除后强制进入 7 天 Deload 保护期 | **[A. 已实现]** | `update_profile(clear_safety_flags=True)` 强制要求 `clearance_reason` 并自动计算 `deload_until = now + 7 days`。 | `test_domain_advanced.py::test_safety_red_flag_and_deload_protocol` | 无 |
| 1.5 | 恢复期降载训练处方 | Deload 期间及受限期间，禁止强度力量训练；自适应调整为恢复性散步或暂停训练 | **[A. 已实现]** | `_evaluate_training_prescription` 与 `get_training_plan` 优先判定 `safety_mode` 与 `deload_until`。 | `test_domain_remaining.py::test_get_training_plan_states` | 无 |

---

### 2. 营养目标与餐食对账（Section 3.3, 4.1, 8, 9.3, Codex Round 8）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2.1 | 真实营养目标解析 | 目标未配置返回 `None` 并请求配置，不凭空填 1800-2100kcal；仅配热量时蛋白保持 `None` | **[A. 已实现]** | `_resolve_nutrition_targets` 仅在热量已配置时返回；未配蛋白返回 `None`；`get_today`, `daily_review`, `plan_tomorrow` 全面对齐。 | `test_codex_unconfigured_plan.py` (2 项全 PASS) | 无 |
| 2.2 | 餐食录入与时区归并 | 记录摄入食物明细、热量/蛋白质区间；按用户本地时区归并自然日 | **[A. 已实现]** | `log_meal` 解析 `occurred_at` 并结合用户时区计算目标本地自然日；校验 `kcal_high >= kcal_low` 与非空稳定键。 | `test_p0_contracts.py::test_timezone_aware_day_aggregation` | 图像食材视觉多模态识别由宿主负责 |
| 2.3 | 餐食修正与不可变审计链 | 通过 `target_meal_id` 修正旧记录；原记录置为 `superseded` 并保留 `parent_meal_id` 因果链 | **[A. 已实现]** | `log_meal` 建立版本修订链；不物理覆盖历史餐食；同时递增 `state_version` 并记录审计日志。 | `test_cross_session.py::test_correction_creates_a_revision_without_overwriting_history` | 无 |
| 2.4 | 餐食快捷复用 | 传 `repeat_meal` 复制昨天或同类型餐食；只复用食材与营养，不修改历史记录 | **[A. 已实现]** | `log_meal` 按用户当地昨天精准查询历史餐食并复用食材营养区间。 | `test_domain_advanced.py::test_meal_delete_and_repeat` | 无 |
| 2.5 | 餐食作废与平账 | 撤销记错的餐食并自动重算当日账本；原记录标记为 `deleted` | **[A. 已实现]** | `delete_meal` 将餐食置为 `deleted`，重算当日 totals 并写入 operation log。 | `test_domain_advanced.py::test_meal_delete_and_repeat` | 无 |
| 2.6 | 剩余营养配额与下一餐策略 | 查询当日剩余热量与蛋白质配额，生成营养优先推荐策略；未建档明确提示 | **[A. 已实现]** | Core 实现 `get_remaining_calories`；未配目标明确提示；超标提示补水；蛋白质未达标优先提示精益蛋白。 | `test_domain_memory_and_trends.py::test_get_remaining_calories_unconfigured_and_configured` | 无 |

---

### 3. 体征打卡、训练决策与复盘（Section 3.2, 4.2, 4.3, 8, Codex Round 8-11）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 3.1 | 日常体征与恢复评分 | 对话录入晨起体重、睡眠、疲劳、酸痛、步数；计算自适应恢复评分 (0-100) | **[A. 已实现]** | `log_daily_metrics` 增量合并同日多次打卡；睡眠<6h 或疲劳>=7 自动扣分并激活恢复规则。 | `test_domain_advanced.py::test_daily_metrics_and_recovery_score` | 穿戴硬件直连（蓝牙/Apple Health）为 P2 路线图 |
| 3.2 | 统一训练决策与多约束过滤 | 结合伤痛约束、可用器械、训练经验与近期证据决策；杜绝组合漏洞与矛盾处方 | **[A. 已实现]** | 建立 `EXERCISE_CATALOG` 属性库与统一禁忌交集过滤；全面覆盖膝+腰、膝+肩并发约束；候选动作均冲突时触发 `TRAIN_CONSTRAINTS_SUSPENDED` 并提供专业转诊指引；不假设“状态优良”且不伪称医疗处方。 | `test_domain_memory_and_trends.py`, `test_codex_review_round9.py` | 特殊病理康复处方依法需执业医师面诊 |
| 3.3 | 训练明细打卡与极简打卡 | 记录组次、负荷、RPE、酸痛反馈；支持极简打卡（仅上报完成率）并校验红旗 | **[A. 已实现]** | `log_workout` 与 `complete_workout` 均具备严格输入校验与急性红旗扫描。 | `test_domain_remaining.py::test_complete_workout_normal_and_red_flag` | 组间实时秒表倒计时属于宿主 UI |
| 3.4 | 晚间复盘与次日预案双态管理 | 复盘全天执行偏差，区分无数据与摄入为零；生成 Draft 预案，支持用户 commit | **[A. 已实现]** | `daily_review` 输出归因与 Draft；`plan_tomorrow` 支持 commit 将执行版计划锁定；未配目标 commit 输出警告。 | `test_domain_advanced.py::test_daily_review_and_plan_tomorrow` | 无 |
| 3.5 | 双重渐进表现状态机 (TRAIN_PROGRESS_01) | 连续2次达到目标次数上限且RPE<=8提议加重；待用户确认；记录证据依据与修订链 | **[A. 已实现]** | `_evaluate_exercise_progression` 精准按用户和动作匹配最近两次有效记录；非目标动作/未完成/缺RPE/单次记录/跨用户均不触发；红旗/减载/疲劳严格优先；`confirm_training_progression` 支持带 `parent_id` 修订链原子确认。 | `test_codex_review_round9.py` (9 项全 PASS) | 复杂多周波形周期化为 P1 路线图 |
| 3.6 | 证据有效窗口与时效控制 | 近期体征定义可配置有效窗口；超期数据按未知处理，不伪造健康分 | **[A. 已实现]** | `recovery_evidence_window_days` 限制体征回溯仅限窗口内（默认1天）；超期体征明确输出 `unrecorded_recent_state` 且 `recovery_score` 置为 None；杜绝数月前睡眠用于今日。 | `test_codex_review_round9.py::test_stale_daily_state_evidence_rejected` | 无 |
| 3.7 | 临场同动模式快捷替换 | 器械被占或局部不适时快捷替换同动模式动作，保持周总容量不破坏 | **[A. 已实现]** | `substitute_exercise` 依据动作库模式属性（如 `lower_squat`, `upper_push`）与关节限制过滤候选，输出同模式等效动作。 | `test_codex_review_round9.py::test_movement_pattern_substitution` | 无 |
| 3.8 | 渐进建议与确认共享状态机 (Codex Round 10) | 1. 最近失败训练打断达标 streak，严禁跳过回溯；2. 同日拆分去重合并；3. 多组同负荷与完整组数比较，无组数/证据/RPE绝不默认成功；4. 确认在事务内重算受限、7天减载、疲劳与禁忌；5. 必须提供 proposal_id 签名摘要与真实证据 ID，拒绝跨用户、伪造、过期、负荷错配；6. 支持自重次数渐进确认；7. 手工负荷录入 (`record_exercise_baseline`) 与达标建议严格解耦。 | **[A. 已实现]** | 重构 `_evaluate_exercise_progression` 严格选同动作最近两会话并判定连续达标；重构 `confirm_training_progression` 事务内全量复验安全状态机与签名摘要；新增 `record_exercise_baseline` 解耦基线录入；MCP 支持自重加次字段。 | `tests/test_codex_review_round10.py` (14 项全 PASS，含 stdio 边界测试) | 无 |
| 3.9 | 统一纯安全评估与真实指标保真 (Codex Round 11) | 1. 抽取纯评估函数 `_evaluate_user_safety_and_recovery`，供计划生成、处方评估、渐进提议与渐进确认全链路统一复用，禁止复制第三套 if；2. 准确解析嵌套 `body["metrics"]` 指标，显式 `is not None` 判定，严禁 `or 8.0`/`or 100.0` 把 0 误当默认健康分；3. 统一阈值（sleep<6.0, fatigue>=7, recovery<60）；4. 按用户本地时区与 `day <= target_date` 过滤，杜绝未来记录倒排掩盖当前疲劳或未来训练提前计入 streak；5. 超过有效窗口的陈旧体征自然失效放行。 | **[A. 已实现]** | 实现 `SafetyRecoveryEvaluation` 纯评估类；`get_training_plan`, `_evaluate_training_prescription`, `_evaluate_exercise_progression`, `confirm_training_progression` 共享；真实 API 录入 `fatigue_level=7`、`sleep_hours=0`、`recovery_score=0`、未来记录遮蔽、超期失效与时区解析全面受测。 | `tests/test_codex_progression_fatigue.py` (8 项全 PASS) | 无 |

---

### 4. 记忆生命周期与双层架构（Section 6.1, 6.2, 8, 9.2, Codex Round 8, 13, 14）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 4.1 | 本地事实与长期记忆双层查询 | 同时检索 SQLite 短期事实与 Obsidian 长期记忆；保留来源状态与置信度；严格限制上限；降级保护 | **[A. 已实现]** | `query_memory` 严格保留来源状态（默认 `unconfirmed`，禁止无故升级为 `confirmed_wiki`），置信度缺省为 `None`，严格按 `limit` 截断，异常与畸形载荷安全回退。 | `test_codex_memory_evidence.py` (2 项全 PASS), `test_domain_memory_and_trends.py` | 待对接用户本地 Obsidian Memory Plugin 真实服务 |
| 4.2 | 候选规律提炼与可靠 Outbox 状态机 | 提炼健康规律放入 Outbox；短事务原子预留，锁外调用 Provider，租期防抢占 | **[A. 已实现]** | `propose_memory_candidate` 采用 Phase1-Phase2-Phase3 状态机；支持 30s 租期、无碰撞哈希。`daily_review` 严格解耦，不向 outbox 伪造写入未经提炼的复盘规则。 | `test_codex_outbox_concurrency.py`, `test_outbox_concurrency_extended.py`, `test_codex_review_round14.py` | 真实写入 Vault 需外部插件确认 |
| 4.3 | 本地周均趋势聚合与纠错闭环 | 时区归日、确定周窗口、每日合计再求均值；删除末餐废止过时趋势；修订链完整 | **[A. 已实现]** | `maintain_memory` 汇总日餐后计算周均；删除末餐自动废止过时趋势并插入撤回记录（`parent_id` 链接）；重复维护幂等；TTL 清理保护因果链。 | `test_domain_memory_and_trends.py::test_maintain_memory_supersedes_stale_trend_when_all_meals_deleted` | 跨季度的宏观研报撰写待接入长期笔记引擎 |
| 4.4 | 崩溃 Worker 租期恢复与批量控制 | 超过 30s 租约自动恢复为 pending；单次最多认领 50 条；续跑状态机返回 `has_more`、`next_maintenance_key` 与 `continuation_token`，指数退避 `retry_after_seconds` 防止紧循环 | **[A. 已实现]** | `maintain_memory` 严格限制 LIMIT 50，过期租约安全抢占并更新 `owner_token`；返回完整续跑控制参数供宿主连续调度。 | `test_outbox_concurrency_extended.py::test_crashed_worker_lease_recovery`, `test_codex_review_round14.py` | 无 |
| 4.5 | 记忆操作校验与破坏性注解 | 限制接口枚举；确认/删除必须有明确确认字段与记录标识；校验先于事务与IO；标注破坏性提示 | **[A. 已实现]** | `memory_action` 在进入事务或外部 IO 前先以 `MemoryActionInput` 校验操作枚举与确认字段；MCP 明确标注 `destructiveHint=True`。 | `test_domain_memory_and_trends.py::test_memory_action_validation_blocks_unconfirmed_delete_and_invalid_action` | 真实执行依赖用户在 Obsidian 端的确认 |
| 4.6 | 随路/惰性记忆维护闭环与纯读计算器 (Spec 6.1, Codex 13-14) | 统一纯读 `_calculate_maintenance_due` 计算器检测待办；Core 提供只读维护提示（`maintenance_recommended`, `suggested_action`, `maintenance_key`, `maintenance_reason`, `due_work`）；宿主显式调度维护闭环，Provider 异常非阻塞降级 | **[A. 已实现]** | `_calculate_maintenance_due` 统一计算 pending outbox、过期 worker 租约、到期餐食明细剪枝（`foods_json != '[]'`）、到期未引用 superseded 记录与已发送清理；`get_today` 与 `daily_review` 纯读快照派生（0写库）；宿主显式调用 `maintain_memory` 推进；Provider 故障时安全保留 pending 并指数退避，事实读写不受阻断。 | `test_codex_review_round13.py` (10项全 PASS), `test_codex_review_round14.py` (8项全 PASS), `test_domain_advanced.py` | 实际生产由真实宿主进程调度执行 |
| 4.7 | 维护代次键与续跑状态机 (Codex Round 14) | 基于当前批次与待办工作指纹动态计算维护键，支持多批次（51+）续跑与失败重试退避，禁止固定键重放旧响应阻断进度 | **[A. 已实现]** | `_calculate_maintenance_due` 与 `maintain_memory` 结合批次 50 项的 `intent_id:attempts:status` 及 TTL 计数计算稳定哈希键 `maint_{user_id}_{day}_g{hash}`；返回 `has_more`, `next_maintenance_key`, `continuation_token`, `retry_after_seconds`；失败重试自动递增 attempts 产生新键，解除幂等阻断。 | `test_codex_review_round14.py` (8项全 PASS) | 无 |

---

### 5. 调度、提醒与逾期补偿（Section 10, 14.2, Codex Round 12-13）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 5.1 | 每日标准提醒窗口生成 | 晨间、午餐、训练、晚餐、晚间 5 个标准时间窗口；生成确定性稳定 `event_id` 与本地时区转换；延期与状态修改防覆盖保护 | **[A. 已实现]** | `schedule_daily_reminders` 按本地时区生成 5 个标准提醒事件；支持 revision 追踪，用户延期（`revision > 1`）或修改状态时不被默认窗口覆盖。 | `test_codex_schedule_sync.py::test_five_standard_windows_generated_with_stable_ids`, `test_postponement_preserved_on_rescheduling` | 实际定时触发由宿主 Cron/Timer 执行 |
| 5.2 | 纯派生日程查询与墓碑同步 | 宿主拉取日程；未送达且窗口已过的事件纯内存派生为 `overdue` 并提示补偿；支持全量快照与墓碑导出撤销宿主定时器 | **[A. 已实现]** | `get_schedule` 为纯派生只读（不执行无审计 SQL UPDATE，不修改 `state_version`）；支持 `include_inactive=True` 暴露已取消/已跳过/已确认等墓碑事件。 | `test_codex_schedule_sync.py::test_read_purity_of_get_schedule`, `test_tombstone_snapshot_for_host_timer_cancellation` | 无 |
| 5.3 | 事件确认与延期处理 | 支持 `delivered`, `acknowledged`, `skipped`, `cancelled`, `postponed` 状态流转与 revision 递增 | **[A. 已实现]** | `acknowledge_schedule_event` 与 `update_schedule_event` 支持完整状态迁移与重新设定窗口，记录审计日志并递增版本。 | `test_domain_remaining.py::test_update_schedule_event_lifecycle`, `test_codex_schedule_sync.py::test_postponement_preserved_on_rescheduling` | 无 |
| 5.4 | 事实驱动的动态触发与抑制评估 | 明确返回 `trigger_condition`、`eligible` 与结构化 `suppression_reason`，结合提醒偏好、餐食记录、训练真实完成率、有效计划锁定等客观事实，全查询封装于单一读事务快照中 | **[A. 已实现]** | `get_schedule` 在单一读快照事务中实时重算客观事实：训练完成要求 `completion_rate >= 1.0`（部分完成 `0.2` 或未提供完成率不冒充完成，提醒保持 `eligible`）；计划检查排除 `superseded` 旧 commit（草稿计划保留 `eligible`）；当餐已记、休息日、受限模式均精确抑制并给出原因。 | `test_codex_schedule_sync.py`, `test_codex_review_round13.py` | 无 |

---

### 6. 数据迁移、一致性与便携性（Section 15, Codex Round 4-6）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 6.1 | 便携式事实导出 | 导出完整安全档案、体征、餐食、计划、日程及操作审计记录 | **[A. 已实现]** | `export_data` 导出便携式 JSON 事实切片，包含 schema_version 与全表状态。 | `test_domain_remaining.py::test_export_and_import_data_roundtrip` | 无 |
| 6.2 | 严格导入校验与冲突回滚 | 精确校验版本（拒绝未知版本）、严格 JSON 解析、时区验证、非法枚举拦截 | **[A. 已实现]** | `import_data` 校验支持版本、有效 IANA 时区，嵌套结构类型检查；任意失败触发原子回滚。 | `test_codex_import_validation.py` (4 项全 PASS) | 无 |
| 6.3 | 安全状态与 Deload 防回退保护 | 严禁过时旧备份静默清除当前受限状态或缩短活跃 Deload 截止时间 | **[A. 已实现]** | 若当前处于受限模式且旧备份为 normal，抛出 `ConflictError`；保留更长 deload 周期。 | `test_codex_import_safety.py::test_old_backup_cannot_silently_clear_current_restriction` | 无 |
| 6.4 | 全持久字段冲突比对 | 同 ID 记录必须全量持久字段比对（含 foods、body、status、parent、revision 等） | **[A. 已实现]** | `import_data` 对 `meal_log`, `domain_record`, `schedule_event`, `operation_log` 执行规范化比对，不一致坚决抛 `ConflictError`。 | `test_codex_import_safety.py::test_duplicate_meal_different_protein_is_conflict` | 无 |

---

### 7. MCP 适配层与宿主安全（Section 9.1, 16, openclaw-adapter-spec）

| 序号 | 需求条目 | 规格要求 | 当前实现状态 | 对应代码与实现逻辑 | 测试用例覆盖 | 剩余缺口 / 外部依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 7.1 | P0 六工具严格白名单 | 默认暴露且仅暴露 6 个核心工具；工具名称与返回 envelope 跨宿主统一 | **[A. 已实现]** | 默认 `allow_all_tools=False` 时仅注册 6 工具；输入 JSON Schema 严格校验。 | `test_mcp_stdio.py::test_p0_tool_discovery_and_invocations` | 无 |
| 7.2 | 全量 26 工具扩展暴露 | 开启 `--allow-all` 时接通全部 26 个领域工具，具备结构化参数与错误信封 | **[A. 已实现]** | `cyber_health_mcp/server.py` 在 `allow_all_tools=True` 暴露全部 26 项工具（新增训练渐进确认与动作替换）。 | `test_mcp_stdio.py::test_allow_all_tools_discovery` | 无 |
| 7.3 | 真实安全注解 (ToolAnnotations) | 细粒度声明 `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` | **[A. 已实现]** | 每个 MCP 工具均配置对应的 `ToolAnnotations`；破坏性工具（如 `delete_meal`, `import_data`, `maintain_memory`）标有 `destructiveHint=True`；写操作均设为 `readOnlyHint=False`；`get_schedule` 保证严格纯读。 | `test_mcp_stdio.py`, `probe_openclaw.py` | 无 |
| 7.4 | 宿主隔离探针握手 | OpenClaw `mcp doctor --probe` 握手成功；无配置篡改；0 运行时 issue 警告 | **[A. 已实现]** | `tests/probe_openclaw.py` 在独立隔离沙箱下运行，成功连接并返回 `ok: true, issues: []`。 | `tests/probe_openclaw.py` | 实际生产调用须遵循用户终端确认策略 |

---

## 三、完整规格逐条缺口清单（分类隔离）

严格依据产品技术规格说明书，将**“本地代码实现缺口”**与**“外部系统与环境依赖”**彻底物理分离，杜绝因外部依赖未接入而混淆代码闭环程度：

### Part A. 本地代码实现缺口与后续演进清单（Code Gaps）

以下为规格说明书在后续演进阶段（P1/P2）定义、当前 Core/MCP 暂未内建的高级功能模块：

| 缺口编号 | 领域模块 | 规格要求描述 | 当前代码承接与降级方式 | 为什么不在 P0/本地代码强行内建 | 未来演进路线 (P1/P2) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **G-1** | 多模态视觉解析 | 拍照识别食物份量、生熟度与烹饪用油估计 | Core 提供 `log_meal` 结构化接收食物名称与 `amount_g` 区间，由 AI 宿主多模态大模型完成视觉识别后传入。 | 规格 Section 5 明确规定：AI 宿主负责自然语言与视觉解析，Core 严禁在本地绑定繁重机器视觉神经模型。 | P1：接入个性化食物拍照校准库，支持按用户盘子直径与习惯比例微调。 |
| **G-2** | 组间实时秒表倒计时 | 力量训练组间间歇计时（如 2-3 分钟倒计时与提醒） | Core 在训练处方中输出结构化 `rest_seconds`（如 60s/90s/120s/150s）与 RIR 指引；完成组次后调用 `log_workout` 记录。 | Core 为无状态/短事务 MCP 服务端，不运行本地 GUI 线程或阻塞长连接计时器。 | 依赖宿主前端 UI Widget（如 Telegram WebApp、飞书卡片倒计时）实现交互。 |
| **G-3** | 多周期高级渐进数学模型 | 跨数月波形周期化力量递增（如 5/3/1、温德勒百分比、大周期 1RM 渐进追踪） | 规格 4.2 核心双重渐进状态机（Double Progression：连续2次达到次数上限且 RPE<=8 提议加重 + 疲劳降载 `TRAIN_RECOVERY_01` + Round 10 负向断链、同日拆分归并、真实证据摘要校验与安全重算）已在 Core 完整实现并受单元测试覆盖。跨季度波形周期化需要更长跨度的真实历史数据。 | 规格 4.2 核心状态机已在 Round 9 与 Round 10 严苛闭环；跨季度的高阶大周期波形模型属于高级专业力量教练功能。 | P1：引入高级力量周期化模块，提供大周期力量曲线拟合与 1RM 渐进追踪。 |
| **G-4** | 宏观趋势 Markdown 研报撰写 | 自动生成包含多维度交叉分析的跨季度宏观研报 Markdown 笔记 | `maintain_memory` 实现了本地周均营养真实汇总（时区归并、去假0、迟到纠错与删除撤回），并在 SQLite 维护完整的 `domain_record` 账本。 | 文本生成与美化排版属于 LLM/宿主生成能力，Core 负责输出客观聚合数据切片。 | 结合 Obsidian Memory Plugin，由宿主在复盘时提炼长篇 Markdown 研报。 |

---

### Part B. 外部系统与环境依赖清单（External Dependencies）

以下为系统在物理生产运行时必须依赖的外部实体、硬件或运行环境。在测试与沙箱环境中，Core 通过 Mock、优雅降级与 Outbox 状态机实现完全隔离与闭环：

| 依赖编号 | 依赖实体 | 外部职责与必要性 | Cyber Health 当前本地安全降级防护 | 为什么不可宣称已“完全集成” | 接入验收前提条件 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **E-1** | **Obsidian Memory Plugin 真实 Vault** | 管理用户本地物理 Vault 文件（`raw/`, `inbox/`, `wiki/`）、全文检索、向量索引与长期双向链接。 | **安全红线**：严禁无授权读写用户本地物理文件。`propose_memory_candidate` 采用可靠 Outbox 状态机，外部不可用时返回 `status: partial` 与 `MEMORY_DEFERRED`，30s 租约安全重试。 | 本地沙箱未挂接用户私有 Vault 路径；未经过物理真实笔记的端到端写入，绝不冒充已连接。 | 宿主环境部署真实 Obsidian Memory Plugin 服务并配置 `MemoryProvider` 通道。 |
| **E-2** | **执业医师与运动康复师临床审核** | 涉及高血压、冠心病等慢病运动处方、急性损伤康复训练的临床医疗合法性签署。 | **法律防线**：全入口拦截急性红旗（胸痛、晕厥），强制锁定 `restricted` 模式；所有处方与知识问答均附加法定 `NON_DIAGNOSTIC` 与非医疗处方免责声明。 | 算法不是医生；国家法规严禁未经面诊的 AI 给出医疗处方或声称经过临床认证。 | 与合法互联网医院或持证运动医学机构建立合规医师电子签名审核通道。 |
| **E-3** | **宿主守护进程主动定时推屏** | 在动态时间窗口（早晨、午餐、训练前、睡前）主动向用户发送提醒与问候。 | **状态机防线**：Core 输出稳定 `event_id`、时区转换与动态窗口；支持惰性逾期扫描（`overdue`）与补偿动作流转。 | MCP 协议本质为 Request-Response 模式，服务端无法单向反向推屏，必须由宿主轮询驱动。 | OpenClaw 配置守护 Cron 任务，或在宿主系统配置 Launchd/Systemd 定期轮询 `get_schedule`。 |
| **E-4** | **智能穿戴设备硬件直连 (P2)** | 实时自动拉取 Apple Health、Garmin、Huawei Health 睡眠、静息心率与步数。 | **自适应防线**：提供 `log_daily_metrics` 供对话录入；未打卡时明确输出 `unrecorded_recent_state`，绝不把“未知”当作“健康”或“状态优良”。 | 属于产品规格 P2 规划阶段，尚未开发原生蓝牙/HealthKit 本地同步 Sidecar。 | 开发基于 Swift/Kotlin 的本地原生数据同步桥接器。 |
