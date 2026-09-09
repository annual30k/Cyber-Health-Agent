# 第十三轮：日程完成证据与维护宿主闭环

112项测试独立复跑通过。保持Gemini3.8，保留独立断言与真实配置/数据隔离。

1. get_schedule 的workout_pending只查当天是否存在workout/workout_log，就当workout_already_completed；即使completion_rate=0.2也永久抑制训练提醒。需要读取实际完成状态，与训练事实统一语义，未知/部分完成不冒充完成。结合当日有效计划（不能拿superseded旧commit），区分部分完成/取消/完成。增测真实complete_workout部分完成、无完成率、完整完成、旧计划修订；查询所有事实在一个读事务快照内。
2. G-5不应擅自改标P1：用户要求的是原需求规格，6.1是当前闭环。按适配规范以纯只读提示+宿主显式维护实现：get_today和daily_review返回结构化maintenance_recommended、reason、可重用稳定维护键/建议动作，严格不隐式写库。复盘可原子记录维护意图；宿主调用已有维护工具执行，失败可恢复且不会每次读都重复维护。完善宿主操作说明及模拟宿主端到端测试，不配置真实定时器/常驻后台。
3. 避免给模型看建议就宣称自动闭环：报告明确区分Core提示、模拟宿主执行、真实宿主未启用三层。确保maintenance pending/失败不会阻断事实读写；并行读/重复触发的幂等、Provider降级都有测试。

本轮完成后报告测试证据及真正剩余实现/外部接入边界。禁止真实Vault/OpenClaw设置/健康库变更。
