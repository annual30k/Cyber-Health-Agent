# Cyber Health Agent 引导安装规范

这是面向 **执行安装的 Agent** 的唯一新用户流程。它将 Cyber Health 的本地健康事实库、MCP
注册和可选的 Obsidian 长期记忆分开处理。不要把 `obsidian-memory-plugin` 说成 Cyber
Health 的一部分，也不要猜测用户的 Vault、Agent 或现有配置。

## 目标与最小确认

默认让用户只做两类选择：

1. 是否启用长期记忆；
2. 若启用，确认安装 Obsidian（如尚未安装）并提供一个已有或新建的 Vault 路径。

在得到这两项确认前，Agent 可以只读检查本机宿主和已有配置，但不能安装公共记忆插件、写入
Vault、修改其配置或把健康记录说成已写入长期记忆。安装后也不得自动整理 Inbox；长期记忆仍遵守
`Inbox → Raw → Wiki` 和用户触发 ingest 的规则。

## 能力矩阵

| 项目 | OpenClaw | Codex | Hermes |
| --- | --- | --- | --- |
| Cyber Health MCP 自动注册 | 是 | 是 | 是 |
| Cyber Health SQLite 健康记录 | 是 | 是 | 是 |
| MCP 使用已验证的 Obsidian 长期记忆 | 是 | 是 | 是 |
| `obsidian-memory-plugin` 的宿主原生加载入口 | 是 | 是 | 是 |
| 当前自动安装/配置公共记忆能力 | 是（已校验 Release） | 否 | 否 |

表中“自动注册”以对应 CLI 可用、同名 MCP 条目没有归属冲突为前提；缺少一个宿主 CLI 只跳过该宿主，
不表示其他宿主安装失败。

“MCP 使用长期记忆”指 Cyber Health 的 `ObsidianMemoryProvider` 仅读写其受限项目单元；它不把
公共插件当作数据库或 MCP Server。对于 OpenClaw，安装器只接受 GitHub 正式 Release 中带 SHA-256
校验的 tgz，并缓存到 Cyber Health 的安装目录。对于 Codex 和 Hermes，插件由其自身的原生安装流程
管理；Cyber Health 不复制、解压或维护它。不能伪造已连接状态。

## 标准流程

首次安装前，用户从 [最新 Cyber Health Release](https://github.com/annual30k/Cyber-Health-Agent/releases/latest) 下载 wheel 和 `SHA256SUMS`、核对 SHA-256，并按 [README 首次安装](../README.md#first-installation-from-github-release) 用 `uvx --from <wheel>` 临时启动 `cyber-health install --dry-run --json`。确认计划后再运行正式 `install`。`uvx` 仅负责启动安装器，不代替安装器执行 Release 校验、宿主登记或长期记忆预检查。Windows 使用 README 的 PowerShell 命令；不要把 macOS 的 `.venv/bin` 路径发给 Windows 用户。

### 1. 识别宿主，不猜测目标

检查 OpenClaw、Codex 和 Hermes CLI 是否可用，以及固定 MCP 名称 `cyber-health` 是否已经存在。
只管理经命令和安装路径验证为 Cyber Health 所有的同名条目；同名但来源不明时停止并报告，绝不覆盖。

不要因为某个 CLI 缺失而中止其他宿主的 Cyber Health MCP 安装。普通模式使用：

```sh
cyber-health install --json
```

面向其他用户时，`cyber-health` 本体也必须来自已发布的 Core Release wheel；安装器会验证
Release 标签、wheel 文件名和 SHA-256，随后缓存到 `~/.cyber-health/releases/`。不得让用户依赖
本地源码目录或 Git 分支。`--project-root` 仅用于明确的开发调试。

这会安装本地 Cyber Health Core，并为发现到的宿主登记 MCP。没有长期记忆授权时，不安装、禁用、
更新或配置 `obsidian-memory-plugin`。

### 2. 只问一次长期记忆

使用以下含义完整的确认，而不是把插件、Vault 和模型处理拆成多次模糊提问：

> 是否启用长期记忆？启用后，Cyber Health 会在你选择的 Obsidian Vault 内创建独立的私有项目目录；
> OpenClaw 存在时会下载并校验最新正式版公共 `obsidian-memory-plugin`；Codex 与 Hermes 的公共插件
> 按该插件自身的宿主说明安装。
> Vault 中被读取的内容可能会进入当前 Agent 的模型上下文；不会读取或改写其他项目。若同意，请提供
> Vault 的绝对路径。

用户拒绝或暂不决定时，执行普通模式并说明：健康记录仍安全保存在本机 SQLite；长期记忆写入将显示为
`MEMORY_DEFERRED`，而不是“已保存”。

### 3. 长期记忆的前置检查

用户同意后：

1. 检查 Obsidian 应用是否已安装。未安装时，提示用户安装；不要在未确认的包管理器中自行下载应用。
2. 要求用户提供一个绝对 Vault 路径。不得从当前窗口、工作目录、最近文件或另一个 Agent 配置中推断。
3. 先运行 dry-run：

   ```sh
   cyber-health install --memory-vault "/absolute/path/to/Vault" --dry-run --json
   ```

4. 若结果为 `memory_bootstrap.obsidian_action: "install-required"`，仅报告需要安装 Obsidian；此时
   Vault、插件和宿主配置均应保持不变。
5. 若 OpenClaw 不可用，仍可初始化 Cyber Health 的受限 Vault 项目并让 MCP 使用它；Codex 与 Hermes
   的通用插件仍需按其 [独立 README](https://github.com/annual30k/obsidian-memory-plugin) 手动安装，
   不得声称该插件已配置。

### 4. 执行、验证与收据

仅在 dry-run 无拒绝项、用户的长期记忆确认仍有效时执行：

```sh
cyber-health install --memory-vault "/absolute/path/to/Vault" --json
```

成功时，Agent 必须逐项验证并向用户给出简短收据：

- Obsidian 已发现；
- OpenClaw 公共插件是“已复用”或“刚安装”（如 OpenClaw 可用）；
- Hermes 公共插件按其原生安装说明另行管理（如 Hermes 可用）；
- OpenClaw 的 `agentConfigs.health-manager` 指向用户选择的 Vault（如 OpenClaw 可用）；
- Cyber Health 仅拥有 `20-Projects/Cyber-Health-Agent-<stable-id>/`；
- `projects.yaml` 只新增该私有项目（不存在时只创建包含该项目的最小 registry）；
- 已注册的 MCP 宿主及长期记忆状态。

不得报告其他 Agent 的项目、笔记、配置值或 Vault 内容。若已有 `health-manager` 绑定到不同 Vault/
项目，或发现符号链接、损坏的 registry、配置在计划后变化，必须失败关闭，不得“修复”为新的绑定。

## 公共插件与 Vault 的边界

`obsidian-memory-plugin` 是可被其他 Agent 继续使用的公共插件。Cyber Health 获得长期记忆授权后，
只能做以下最小变更：

```text
Vault/
├── 00-System/projects.yaml                         # 仅追加 Cyber Health 项目映射
└── 20-Projects/Cyber-Health-Agent-<stable-id>/     # Cyber Health 独立私有范围
    ├── AGENTS.md / rules.md / index.md / log.md
    ├── inbox/  raw/  wiki/  checkpoints/
```

禁止读取、移动、重命名或清理其他 `20-Projects/*`、`10-Global/`、`30-Shared/` 或用户已有根目录内容。
禁用或卸载 Cyber Health 也不得卸载公共插件、删除 Vault 或修改其他 Agent 绑定。

## Codex 与 Hermes 的准确呈现

- **Codex**：Cyber Health MCP 可以自动注册。公共插件拥有 Codex manifest，但当前 Cyber Health
  安装器不会代替用户安装或持久化 Codex 的 Vault 连接；如果用户要求该通用插件在 Codex 中工作，按
  [插件 README](https://github.com/annual30k/obsidian-memory-plugin#codex) 的宿主流程单独确认并执行。
- **Hermes**：Cyber Health MCP 可以自动注册和验证工具发现。公共插件必须按
  [插件 README](https://github.com/annual30k/obsidian-memory-plugin#hermes) 的 Hermes 原生流程安装；
  Cyber Health 不写入 `$HERMES_HOME`、不覆盖 Skill，也不写入其环境变量。
- **OpenClaw**：在用户已同意长期记忆、Obsidian 与 Vault 均通过检查后，安装器可安装缺失插件，并仅
  合并 `health-manager` 的 `agentConfigs` 条目及必要 Hook 权限。

## 失败时的用户语言

| 状态 | 正确说法 |
| --- | --- |
| 用户未启用长期记忆 | “Cyber Health 已安装；健康记录保存在本机。长期记忆尚未启用。” |
| `install-required` | “尚未改动 Vault 或插件。请先安装 Obsidian，然后重新选择这个 Vault。” |
| `MEMORY_DEFERRED` | “健康记录已保存到本机；长期记忆暂未同步，之后可修复连接并重试。” |
| 外部插件配置冲突 | “检测到已有不同的长期记忆绑定；为保护现有项目，我没有修改它。” |
| OpenClaw 不可用、Hermes 可用 | “Vault 项目已配置；Hermes 公共插件请按其原生安装说明单独配置。” |
| OpenClaw 与 Hermes 都不可用 | “Vault 项目可供 Cyber Health MCP 使用；Codex 的通用插件仍需手动安装。” |

不要说“已经保存到 Obsidian”“长期记忆已连接”或“已经配置所有 Agent”，除非对应的验证结果明确为
成功。

## 文档优先级

本文件定义新用户安装与确认流程。实现与参数以 [主 README](../README.md) 为准；公共插件自身的
宿主安装、Skill 规则和手动排障以 [插件 README](https://github.com/annual30k/obsidian-memory-plugin) 为准；
OpenClaw 的低层契约以 [适配规范](openclaw-adapter-spec.md) 为准。三者冲突时，不擅自扩展写入权限，
而是停止并报告不一致。
