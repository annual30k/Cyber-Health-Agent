# 第七轮独立审核

59 项既有测试独立复跑通过。第六轮导入四项已修复、24 工具发现与扩展调用已有测试。新增 tests/test_codex_memory_evidence.py 两项失败，请保持 Gemini 3.8 开发、保留全部独立断言。

## 必须修复

1. query_memory 把 Provider 没给 confirmation_status 的结果默认标 confirmed_wiki，并默认 confidence=0.9，凭空制造确认与置信度。改为 unknown/unconfirmed 及未知置信度，保留来源状态；不能把候选自动升级已确认知识。还未限制 Provider 返回数量，且返回格式异常时可能留下部分结果却标未连接。建立完整结果验证/上限/一致性处理，并增测异常项与降级。
2. maintain_memory 所谓周/月趋势实际只是全部历史餐食 AVG，不是每日合计的周均值：一日两餐与两日各一餐会混淆。每天对所有老餐扫描，INSERT OR REPLACE 无修订链覆盖同日趋势，不清理食材明细。请依据原规格第6节与MealLog保留策略，实现确定周/月窗口、时区归日、缺失天数披露、稳定聚合和迟到纠错修订，安全清理明细同时保留可追溯性，不能制造缺失日摄入为0。增加跨周、多餐、重复维护、迟到纠错测试。
3. memory_action 目前任意 action_type 字符串直接转发 Provider，工具宣称可确认/删除却 destructiveHint=False。限制规格接口枚举并验证各操作载荷，确认/删除必须有明确确认字段与记录标识，Provider负责最终权限而Core也不能随意转发；注解应覆盖最危险支持操作，或拆工具。模拟Provider测试拒绝非法方法/无确认的删除，确认前不发生外部IO。

继续按规格补缺口，不把已通过59测试等同全需求完成。真实Provider与临床审阅仍缺；禁止改真实Vault/OpenClaw设置/提醒/健康数据。Codex不并行业务编辑。
