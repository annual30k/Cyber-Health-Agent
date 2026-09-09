# 第十四轮：维护提示必须具备可执行的续跑语义

122项现有测试独立通过。保持Gemini3.8，保留独立测试，禁止真实用户库/配置/Vault写入。

第十三轮仍有三项机制性缺陷：

1. get_today只统计status=pending的outbox决定维护提示，既不检查到期餐食/趋势，也漏过期in_flight租约。没有候选但有过期本地事实时永远不建议维护，不满足惰性补跑。请用统一纯读due-work计算器覆盖TTL、pending、过期lease，并披露原因。
2. maintenance_key固定maint_user_day，而maintain_memory把partial结果记录为完成的幂等响应。第一次失败、一天内第二批新任务、队列>50下一批都再次给同键，永远重放旧响应而不处理剩余任务。设计稳定批次/工作代次+明确可重试续跑token：同一请求安全重放，新的待办/下一批/延迟重试必须可推进。不要靠宿主任意随机key破坏可追踪性，也不要紧循环重试Provider。增测51+任务、partial后Provider恢复、同日新任务、重复get_today、崩溃lease恢复。
3. daily_review直接构造outbox写propose，绕过统一propose_memory_candidate请求状态机且method与已有memory.propose混用。明确统一Provider方法协议与意图身份，复盘维护意图不应冒充已达到证据阈值的长期健康规律。维护调度与候选事实区分，不要每次复盘都自动把全文作为长期规律提交。通过模拟Provider核对实际调用方法、载荷及重复执行。

本轮先正确实现due-work→稳定触发→一次处理→续跑/退避→完成这一完整状态机，统一复盘与查询提示，不继续堆局部if。完成后交接。
