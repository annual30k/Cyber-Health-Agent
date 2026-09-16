# Obsidian Memory Plugin：Skill 插件化实施说明

> 对应插件仓库：[annual30k/obsidian-memory-plugin](https://github.com/annual30k/obsidian-memory-plugin)
> 支持宿主：OpenClaw、Codex、Hermes
> 范围：将已有 obsidian-memory Skill 内置到插件；Obsidian 操作技能由宿主独立安装。

## 1. 已确认的产品形态

本插件的核心是项目中原有的 Obsidian Memory Skill，不另建记忆系统，采用**多宿主一体化**设计：

```text
宿主插件 / Skill 适配 (OpenClaw / Codex / Hermes)
  ├── 内置 obsidian-memory Skill：自生长规则与工作流（100% 共享）
  ├── OpenClaw 适配层：openclaw.plugin.json、Hook 注入 (index.js, lib/)
  ├── Codex 适配层：.codex-plugin/plugin.json、仓库级 marketplace (.agents/)
  └── Hermes 适配层：通过 Hermes 原生插件安装流程加载
             ↓
宿主独立安装完整 kepano/obsidian-skills 套件（运行时按需加载）
             ↓
受限 Vault 文件系统（普通记忆操作）
             ↓
Obsidian CLI → Obsidian 应用（仅应用专属操作）
```

上游 [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) 是
**宿主依赖，不随本插件打包**。本插件不复制这些 Skill、不重新实现它们的操作。
宿主需要完整安装上游套件，不只安装 obsidian-cli、obsidian-markdown。
2026-09-04 的上游清单还包括 obsidian-bases、json-canvas、defuddle；安装时
以所选版本的完整技能清单为准。缺少任一成员时，通过宿主安装流程补齐；按宿主权限要求取得授权，
验证技能可见后再加载使用。不在插件代码中静默下载。

不包含此前讨论的独立 Runtime、MCP、SQLite、自定义 I/O 引擎、细粒度会话绑定
或批次事务机制。本文取代原先过度展开的设计，未保留旧版副本。

## 2. 当前文件与职责

实现位于独立仓库 [obsidian-memory-plugin](https://github.com/annual30k/obsidian-memory-plugin)：

```text
obsidian-memory-plugin/
├── package.json
├── openclaw.plugin.json           （OpenClaw 清单）
├── .codex-plugin/plugin.json      （Codex 原生插件清单）
├── index.js
├── lib/
│   ├── config.js
│   └── prompt.js
├── skills/obsidian-memory/
│   ├── SKILL.md
│   ├── references/
│   │   ├── vault-layout.md
│   │   ├── bootstrap.md
│   │   ├── dependencies.md
│   │   ├── identity-and-recovery.md
│   │   └── templates.md
│   └── assets/templates/  （14 个最小记忆模板）
├── examples/openclaw.config.json
├── tests/
├── README.md
└── SOURCES.md
```

仓库根目录扩展：
- `.agents/plugins/marketplace.json`：向 Codex 提供本地 marketplace 索引，支持 `codex plugin marketplace add .`。

| 模块 | 职责 |
| --- | --- |
| package/manifests / Hermes adapter | 让 OpenClaw、Codex 与 Hermes 分别识别各自的原生插件入口 |
| .codex-plugin | 符合 Codex 官方校验器标准规范的插件元数据与接口声明 |
| index.js | 注册 before_prompt_build，限定配置的 agentId |
| config.js | 校验连接字段，拒绝相对路径、非法 ID、不完整配置 |
| prompt.js | 引导加载内置记忆 Skill，声明宿主技能依赖与连接元数据 |
| SKILL.md | 原 Skill 的召回、选择性暂存、用户触发整理和维护逻辑 |
| vault-layout | 原 Vault 结构与字段契约 |
| bootstrap | 用户明确初始化时，由当前 Agent 建立缺失结构和模板 |
| dependencies | 核对完整上游套件；区分缺失、不可见、被禁用、不满足条件和同名覆盖 |
| identity-and-recovery | 稳定候选/Raw 关联、重复请求和中断恢复规则 |
| templates / assets | 最小字段和空白输出模板，不固定业务分类 |

没有运行时 npm 依赖，没有编译步骤。发布 JavaScript 入口本身，避免为薄封装增加
构建层。包内只有一个 Skill，宿主独立安装的 obsidian-skills 由其自行维护版本。

## 3. 对原 Skill 的保留与必要适配

保留：

- Inbox → 用户触发 ingest → Raw → Wiki/偏好/Checkpoint。
- 只有未来有用、难以从代码/Git/既有规范页恢复的高价值信息才自动暂存。
- 明确“记住”可以暂存，但不会自动整理。
- 原始证据不可由综合结论替代；附件保留原字节。
- 搜索 canonical 页面，按 support/extension/duplicate/conflict/supersession/new concept 融合。
- 首次有价值的整理才建立项目分类；维护 index 和 append-only log。
- 项目隔离、Global/Shared 边界、机密显式保留、凭据值禁存。

必要适配：

- 以用户明确选择的 Vault 文件系统作为普通记忆操作的默认通道；CLI 仅用于
  Obsidian 应用专属能力。
- 连接由插件配置或用户明确选择提供，不内置私人路径。
- 不把 OpenClaw Gateway cwd 或 Agent 通用工作区当成用户代码项目。
- 初始化说明作为 Skill 内部参考，不再要求用户复制整份 bootstrap 提示词。
- 原始参考目录保持不变；可发布许可待所有者决定，当前包为 private/UNLICENSED。

## 4. 配置契约

最小业务配置：

```json
{
  "agentId": "main",
  "vault": "My Vault",
  "vaultPath": "/absolute/path/to/My Vault"
}
```

可选字段：

| 字段 | 默认/行为 |
| --- | --- |
| cliPath | 默认 obsidian，也可填 CLI 可执行文件绝对路径 |
| projectId | 已存在的 Vault 项目 ID；不填时由当前任务明确选择 |
| projectRoot | 实际代码根目录；不填时不使用 Gateway cwd 猜测 |

空对象允许安装，但不会注入连接。非空配置必须提供 `agentId` 与 `vaultPath`；
`vault` 仅作为可选显示名或 CLI 定位提示。
projectId 与 projectRoot 是否对应相同项目由 Agent 连接时检查 projects.yaml；
插件注册本身不访问 Vault。

配置合并到 plugins.entries.obsidian-memory-plugin.config；Hook 权限单独放在
同一插件 entry 的 hooks 下。完整示例见
[安装配置](https://github.com/annual30k/obsidian-memory-plugin)。

如宿主已有 plugins.allow，追加插件 ID，保留其他成员。不得覆盖原配置，
不得修改 memory 插槽或自动禁用现有记忆系统。

## 5. 加载与依赖流程

1. OpenClaw、Codex 与 Hermes 分别通过各自原生插件入口加载本包的 skills 目录；Cyber Health 不复制或解压该目录。
2. 对配置的 agentId，Hook 添加短入口提示和连接元数据。
3. 当前 Agent 读取本包 obsidian-memory/SKILL.md。
4. 配置/安装时核对上游完整套件；使用时只加载相关技能：Markdown 编写笔记、
   CLI 执行应用专属操作、Bases 处理 .base、JSON Canvas 处理 .canvas、Defuddle
   提取网页正文。安装整套不等于每轮都加载所有 Skill 全文。
5. 先区分确认缺失与已安装但不可见/被禁用/条件不足/同名覆盖；只有确认缺失
   才使用宿主支持的技能安装流程，补齐套件内所有缺失 Skill，不重复安装。
6. 安装需要的授权未包含在当前请求里时先获得授权；不任意覆盖已有技能或全局配置。
7. 逐项检查实际来源/可见性/运行条件，再调用相关能力。只有两个技能不能
   算全套就绪；技能文件存在也不代表相关可执行工具已经可用。

Hook 不读取 event.messages，不运行 CLI，不写记忆，不调用模型，不创建后台任务。
工作流实际由当前 Agent 按 Skill 完成，因此“Hook 注册成功”不等于已经完成知识库闭环。

## 6. 自生长操作契约

| 用户意图 | 结果 |
| --- | --- |
| 检查连接 | 加载宿主依赖，核对 Vault 名称/ID和实际路径，只读 |
| 初始化知识库 | 明确授权后按 bootstrap 创建缺失结构，不覆盖用户文件 |
| 绑定当前代码项目 | 最长真实目录匹配；确认未绑定后只建立当前项目 |
| 询问历史决定 | 搜索当前项目 Wiki/Raw/Checkpoint，按来源回答 |
| 记住某事 | 高价值/显式内容进入 Inbox，保持 pending-ingest |
| 记住跨项目个人偏好 | 只进入 Global Inbox；不要求项目，不修改正式偏好 |
| 整理候选或指定来源 | 冻结 Raw，融合知识，更新 index/log，验证后标记 ingested |
| 检查矛盾/断链 | 默认只读报告 |
| 明确要求修复 | 只修授权范围，保留用户修改和来源 |

用户选择的 Vault 路径不可访问或写入失败时必须说明未保存。CLI 不可用时，
应说明应用专属能力不可用，但可继续在已验证的 Vault 路径中执行受限的普通
Markdown 记忆操作。

Raw 不可变是工作流约定，不是文件系统 WORM。一次整理部分失败时，
说明已完成和未完成，继续前检查实际文件，避免重复来源和日志。
新候选使用稳定 `cand-<uuid>` ID，Raw 使用确定性路径，日志使用稳定
`ingest-<candidate_id>` 标记；已完成的重复请求不再写入。Raw 内容不匹配时
停止并报告，不能覆盖证据或换 ID 绕过。兼容旧候选/Raw 关联，不自动迁移。
首版只承诺串行工作流，不提供数据库事务。

固定模板只约束必要的身份、来源、隐私和生命周期字段，业务分类仍在首次
有意义的整理时从真实证据建立。优先使用兼容的 Vault 现有模板，缺失时读
包内模板；仅初始化授权允许复制缺失模板进系统目录，升级不覆盖用户模板。

## 7. 安装与验证

具体步骤和命令见 [插件 README](https://github.com/annual30k/obsidian-memory-plugin)。

开发验证：

```sh
cd /absolute/path/to/obsidian-memory-plugin
npm run check
npm test
npm pack
node tests/openclaw-smoke.mjs obsidian-memory-plugin-<release-version>.tgz
```

最后一项使用本机 OpenClaw 和临时隔离配置读取打包产物，不连接 Vault，
不安装到正在运行的用户 Gateway。它不代替真实对话测试。
完整对话用例见独立仓库中的 `tests/skill-scenarios.md`，
包括重复整理、中途失败、旧 Raw 兼容、Global 路由与依赖隐藏状态；未运行项
必须标为 not-run，不能用静态规则检查替代。

真实闭环仍需在用户授权的测试 Agent/Vault 完成：

1. 完整套件已存在：直接复用，不重新安装；普通任务仅加载相关指令。
2. 仅两项存在或有其他缺失：核对全套清单，按宿主流程补齐全部缺项，
   不重复安装、不自写替代实现；分别验证技能和工具运行条件。
3. “记住”仅创建 Inbox。
4. 用户整理后 Raw/Wiki/index/log 完整且有来源。
5. 新会话能够召回同一知识。
6. 相冲突的来源不会静默覆盖旧结论。
7. 应用无法连接时不误报应用操作成功；已验证 Vault 的普通 Markdown 记忆
   操作仍可完成，且不会越出受限路径。
8. 禁用/卸载仅移除插件，不删 Vault 或宿主的 obsidian-skills。

## 8. 运行与隐私边界

CLI 执行依赖 Obsidian 应用进程；新版本可以自动唤起应用，不要求窗口保持前台。
仅安装应用不等于 CLI 已注册或连接可用，但这不影响已验证 Vault 路径下的
普通 Markdown 记忆操作。每次直接文件操作前均须解析物理 Vault 根目录和目标
路径，拒绝路径穿越与越界符号链接；受管替换写入应尽可能原子化，并在每个范围内串行。

首版用于同机、受信任单用户 Agent。agentId 只是提示注入范围，不是操作系统权限
隔离；不要将有私有 Vault 权限的 Agent 无限制开放给群聊或陌生用户。

本地 Vault 资料在被 Agent 读取后，可能进入宿主配置的模型服务上下文。
不宣传“绝不离开本机”。不保存密码、Token、私钥、恢复码。

当前已完成 OpenClaw、Codex 与 Hermes 三宿主适配，均使用各自原生入口。三者共享底层唯一的
Memory Skill 与宿主已有 Obsidian Skills；Cyber Health 不复制记忆业务逻辑或维护宿主插件文件。

## 9. 验收口径

区分三类结果：

- 静态/单元验证：包结构、引用、配置、Hook 行为。
- OpenClaw 隔离加载：实际宿主识别 tarball 和内置 Skill。
- Hermes 隔离适配：从 tarball 提取同一 Skill，验证连接变量与同名 Skill 冲突拒绝。
- 实机知识库闭环：由真实 Agent 调用宿主 Obsidian Skills 完成，尚需单独验收。

原 Cyber Health 代码和实际 Obsidian Vault 不因本插件封装而改动。
