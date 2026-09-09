# Codex 第五轮独立验收

日期：2026-09-04。尚未通过全需求验收。

## 已复现阻断项

`tests/test_codex_import_safety.py` 两个用例在本轮初始实现失败：

- 正常档案导出后记录胸痛，导入旧备份会将 restricted 清为 normal。恢复备份不得绕过安全解除和过渡流程；应拒绝过时覆盖或保守保留更严格状态。
- 同 ID 餐食修改 protein_high 后导入未抛冲突。所有持久字段需规范化全量比较，不能只比较热量/时间。领域记录与日程也需覆盖 body、status、parent、窗口、revision 等。

需同时验证：精确支持的 schema、完整性摘要、嵌套结构及营养范围、事务失败整体回滚。校验和不是可信认证，不能取代语义校验。保留既有测试断言。

## 文档真实性

不能把 Core 方法存在等同于 MCP 闭环完成。新增训练、迁移接口尚未在现有 14 工具全部暴露；memory_action、双层查询、TTL 趋势压缩需逐项列真实状态。版本文件、锁文件与说明须一致。实际 MemoryProvider 未接通，不能宣称 Obsidian 已集成。

## 真实宿主隔离探针

执行 `.venv/bin/python tests/probe_openclaw.py`，OpenClaw 2026.8.2：

- mcp add 探测并保存成功（仅临时配置）。
- mcp doctor cyber-health --probe --json：ok=true，无错误。
- 信息提示：tools have no safety annotations; calls will require interactive approval。

范围：证明宿主连接探测成功，不证明聊天端业务调用、自动提醒投递、长期记忆集成或全需求完成。默认六工具白名单；数据库、OPENCLAW_STATE_DIR、OPENCLAW_CONFIG_PATH 全部临时隔离。未修改用户正式配置。
