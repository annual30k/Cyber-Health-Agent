# 第十一轮：真正共享安全评估

96项既有测试独立通过；新增 tests/test_codex_progression_fatigue.py 失败，使用真实log_daily_metrics录入睡眠5.9h后，旧加重建议仍确认成功。

根因：confirm_training_progression重复实现安全逻辑，读取ds_body顶层sleep_hours/fatigue_score；实际指标在ds_body.metrics.sleep_hours/fatigue_level。且阈值写成fatigue>=8或recovery<40，与计划入口>=7/<60不一致；`or 8.0`/`or 100.0`还把0误当未知并默认健康。

请保持Gemini3.8，保留独立测试断言，抽取真正共享的安全/恢复评估纯函数（统一结构化metrics、阈值、未知状态、有效窗口、用户本地日期）。计划生成、建议和确认调用同一个函数，禁止继续复制第三套if。确认时查询未来day的最新记录也可能遮蔽当前疲劳，需要按目标本地日过滤，_evaluate_exercise_progression确认date=None也不应引用未来训练。

增测真实接口录入fatigue_level=7、sleep_hours=0、recovery_score=0、未来记录遮蔽当前、过期数据及不同时区。不要用手写错误顶层JSON的fixture代替真实API，所有安全测试应验证真实生产输入链路。完成再交接，禁止触碰真实Vault/健康库/OpenClaw设置。
