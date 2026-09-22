# custom-skills

个人专用 Coding Agent Skills 集合，可被 Pi、OpenCode、GitHub Copilot（CLI）等支持从 `~/.agents/skills` 加载 Agent Skills 的工具复用。

## 前置要求

- **Python >= 3.10**
- **Node.js >= 20**（部分技能需要）
- **uv**（browser-use 与 computer-use 通过它准备隔离运行时；没有时 sync 会跳过对应段）
- **本机安装的 Chrome**（浏览器能力需要；local-web-search 还要求当前 Agent 能操作用户已授权的有界面个人 Chrome Profile，Chrome 不可用时才按工具能力降级到其他已授权的本机有界面浏览器）
- **Windows + PowerShell 7（pwsh）**：项目中的脚本、SKILL 命令示例和 context 文件默认面向 Windows 上的 PowerShell 7 编写，Shell 命令使用 pwsh 7 语法；在其他 OS / Shell 下使用时需自行改写相应命令

## 快速开始

### 一键安装所有技能、context 文件和扩展

```shell
git clone https://github.com/<your-username>/custom-skills.git
cd custom-skills
python scripts/install.py
```

脚本会自动完成：
- 全局技能：依赖下载 → 构建打包 → 部署到 `~/.agents/skills/`
- Context：将 `context/` 下的 `APPEND_SYSTEM.md` 部署到 `~/.pi/agent/`
- 二进制：确保 `grep`、`find` 依赖的 `rg`、`fd` 存在于 `~/.pi/agent/bin/`
- 扩展：将 `extensions/` 下的 Pi 扩展部署到 `~/.pi/agent/extensions/`
- browser-use：通过 uv 安装/升级官方 browser-harness 包，同步 Chrome 专用 SKILL.md、适配脚本和上游参考说明（普通 sync 也刷新，无需 --force）
- computer-use：在独立 Python 3.13 环境安装哈希锁定的 cua-driver SDK，校验绑定和原生文件后同步后台限定桌面技能；不注册启动任务或常驻服务

`project-skills/` 中的项目级技能不会被默认安装，必须显式指定技能名称和目标项目。

### 只安装指定全局技能

```shell
python scripts/install.py --name local-web-search
```

### 将指定项目级技能安装到项目

```shell
python scripts/install.py --project-skills --project-dir 'D:/dev/empty' --name pixel2ase
```

项目级技能部署到 `<project-dir>/.agents/skills/<name>/`。`--project-skills`、`--project-dir` 和 `--name` 必须同时提供；不支持把全部项目级技能批量安装到一个项目。覆盖已有技能时增加 `--force`。

### 覆盖已安装的技能

```shell
python scripts/install.py --force
```

### 只安装指定类别

不加选择参数时全部安装；加 `--skills`、`--context`、`--extensions`、`--binaries`、`--tools` 则只安装对应类别（可组合）：

```shell
# 只更新 context 文件
python scripts/install.py --context --force

# 只安装技能
python scripts/install.py --skills

# 只部署扩展（会连带检查 rg / fd）
python scripts/install.py --extensions --force

# 只检查二进制依赖，有新版时询问是否升级
python scripts/install.py --binaries

# 安装/升级全部工具型技能及其运行时
python scripts/install.py --tools

# 只准备 computer-use 运行时并刷新该技能（不要求 Node/pnpm）
python scripts/install.py --tools --name computer-use

# 只更新 browser-harness 并刷新 browser-use
python scripts/install.py --tools --name browser-use
```

### 自定义安装目录

```shell
python scripts/install.py --skills-dir /path/to/skills --pi-agent-dir /path/to/pi/agent
```

### npm scripts 快捷方式

```shell
pnpm run sync         # 等价于 python scripts/install.py
pnpm run sync:force   # 等价于 python scripts/install.py --force
pnpm test             # 跑全部静态测试（Node + Python）
```

注：这些脚本不能叫 `install`（npm 生命周期钩子，`npm install` 会递归触发），也不建议叫 `setup` / `deploy`（与 `pnpm setup`、`pnpm deploy` 内置命令同名）。

## 技能列表

| 技能 | 作用域 | 说明 |
|---|---|---|
| [browser-use](skills/browser-use/README.md) | 全局 | 个人 Chrome 技能，依赖 browser-harness 并复用其 CDP/helpers，连接 Windows 稳定版个人 Chrome；`sync` 生成说明并刷新适配脚本 |
| [computer-use](skills/computer-use/README.md) | 全局 | Windows 原生 GUI 单步操作技能；直接使用 cua-driver 同进程 SDK，默认后台限定，不启动 MCP Server 或常驻 daemon，保留快照重验证、请求去重和有界 JSON |
| [local-web-search](skills/local-web-search/) | 全局 | 通用本地浏览器搜索工作流；默认使用有界面个人 Chrome Profile、Bing 国际版优先，支持用户手动登录后继续，无独立运行时依赖 |
| [pixel2ase](project-skills/pixel2ase/) | 项目级 | 将 AI 生成的像素风图片转换为原生分辨率 PNG 和 indexed `.aseprite` 工程 |

## 扩展列表

| 扩展 | 提供 | 说明 |
|---|---|---|
| [notify](extensions/notify.ts) | 事件钩子 | 任务完成提醒 |
| [enable-core-tools](extensions/enable-core-tools.ts) | 事件钩子 | 把 SDK registry 里的 `grep` / `find` / `ls` 补进激活集 |
| [tools-status](extensions/tools-status.ts) | `/tools-status` | 本会话各工具的调用 / 完成 / 出错次数 |
| [pwsh](extensions/pwsh/) | `pwsh` | 使用 PowerShell 7 语法执行命令；不修改内置 `bash` 的激活状态 |
| [services](extensions/services/) | `service_start` `service_list`（常驻 loader）· `service_logs` `service_stop` `service_restart`（按需加载）· `/services` | 常驻服务（dev server、后端、watcher）的后台启动与管理 |

### pwsh

`pwsh` 是面向固定 Windows / PowerShell 7 工作流的命令工具，避免工具名叫 `bash`、模型却必须输出
PowerShell 语法的认知错位。扩展从 PATH 和 PowerShell 7 标准安装目录自动发现候选，逐个用
`-NoProfile -NonInteractive` 探测版本，只在主版本 >= 7 时注册工具；不可用时保留其它工具原状并给出通知。

执行层复用 Pi 的 `createBashToolDefinition()`，因此保留流式输出、Esc 取消、超时、Windows 进程树清理、
末尾 2000 行 / 50KB 截断、完整输出临时文件与 `PI_*` 会话环境；公开的工具名、参数说明和提示全部使用
PowerShell 7 语义，工具调用在 TUI 中以 `PS>` 开头显示。

扩展**不会**调用 `pi.setActiveTools()`，也不会覆盖或禁用内置 `bash`。是否关闭 `bash` 属于用户自己的
工具集策略，可在扩展之外单独处理；即使两个工具同时激活，`pwsh` 的 guideline 也会要求模型只向它传
PowerShell 7 语法。

### services

`pnpm dev` 这类命令永远不退出，而 `bash` 工具的语义是「等进程退出、拿退出码」，模型一调就卡住；
就算起来了，之后也没人记得它在哪，用户想手动关都找不到。本扩展把「后台执行 + 句柄 + 单独读日志」补齐：

- 进程以 **detached** 方式启动并 `unref()`，脱离 pi 的进程树；工具调用立即返回，不阻塞对话
- 输出写入 `<project>/.pi/logs/<name>.log`，注册表写入 `<project>/.pi/services.json`
- 就绪探测三选一：`port`（TCP 可连）> `readyLog`（日志正则）> 固定等待；超时不算失败，直接把日志尾部返回给模型自己判断
- 运行中的服务经 `ctx.ui.setStatus` / `ctx.ui.setWidget` 展示，两者都是 SDK 原生、宿主无关的接口：
  CLI 画在 footer 与编辑器上方，pi-agent-chat 画在状态行与输入框上方，扩展本身不依赖任何特定宿主
- 用户通道是 `/services list | logs <name> | stop <name> | restart <name>`，与模型用的工具共用同一份实现

**工具按需加载**（Pi 的 Dynamic Tool Loading）：五个工具全部注册，初始只激活两个入口 loader。
`service_start` 负责创造服务；零参数、低 schema 成本的 `service_list` 负责查实况，并把已停止服务的
可读日志名也列出来。二者按结果用 `pi.setActiveTools()` **纯增量**追加另外三个工具：有活服务时放出
`service_logs` / `service_stop` / `service_restart`，只有历史日志时仅放出 `service_logs`，什么都没有就不放。
Pi 把新增工具名记在本次工具结果上；支持 deferred loading 的模型在结果位置加载定义，不动缓存前缀，
其它模型在下一次请求的工具列表里拿到。会话开始一律收回三个懒加载工具，避免继承上个会话的状态。
三个懒加载工具刻意不带 `promptSnippet` / `promptGuidelines`：那类元数据会重建系统提示，反而吃掉缓存收益。
纯逻辑在 `core.ts` 的 `lazyToolsFor()` / `planToolLoad()` / `planToolReset()`，有静态测试覆盖。
用户通道 `/services` 不受影响：命令不是工具，任何时候都可用。

**pid 安全**：操作系统会复用 pid，所以「pid 还活着」不等于「还是我们那个进程」。
所有会杀进程的路径都先比对进程创建时间：对不上就从注册表剔除，比对不了则**拒绝自动停止**并把手动命令告知用户——
杀错一棵进程树的代价远大于多一次确认。

**Windows 启动链**：命令默认跑在**与 `bash` 工具相同的 shell**（Windows 上是 PowerShell 7），
所以服务命令和普通 Shell 命令用同一套语法，不存在方言切换；`pwsh` 不在 PATH 上时退回
 cmd.exe，并在 `service_start` 的返回里明确提示。需要别的 shell 时传 `shell` 参数覆盖。

Windows 上中间多一层 node 启动器进程，这不是多余设计，而是三个实测结论逼出来的
（详见 `core.ts` 的 `shellInvocation` 注释）：Node 的 `shell` 选项与 `detached` 不兼容、
pwsh 直接跑在无 console 的 detached 进程里会立即退出、
cmd.exe 在 detached 下不把子进程的 stdio 转发到继承的文件句柄（日志永远为空）。
套一层 node 后，pwsh 变成启动器的普通子进程，console 与 stdio 均正常，
`killTree` 会把 node → pwsh → 服务整棵树清掉。POSIX 不受影响，保持原生 `sh -c`。

建议把 `.pi/logs/` 和 `.pi/services.json` 加入目标项目的 `.gitignore`。

## 目录结构

```
custom-skills/
├── scripts/
│   └── install.py            # 全局安装脚本
├── context/
│   └── APPEND_SYSTEM.md     # 追加到 Pi 系统提示词末尾，定义全局行为规则
├── extensions/
│   ├── notify.ts            # Pi 扩展 → ~/.pi/agent/extensions/
│   ├── enable-core-tools.ts # 同上：激活 grep / find / ls
│   ├── tools-status.ts      # 同上：/tools-status 工具使用统计
│   ├── pwsh/                # 同上：PowerShell 7 命令工具（pwsh.ts 为入口）
│   └── services/            # 同上：常驻服务管理（目录形式，services.ts 为入口）
├── tests/
│   ├── *.test.mjs           # 扩展的静态测试（不部署）
│   └── test_*.py            # 技能与安装逻辑的静态测试
├── skills/                    # 默认安装到全局目录
│   ├── browser-use/           # 个人 Chrome 技能，依赖 browser-harness
│   └── <skill-name>/
│       ├── SKILL.md           # 技能定义（Agent 读取）
│       ├── README.md          # 人类可读说明
│       ├── package.json       # 依赖声明（如需要）
│       ├── scripts/           # 可执行脚本
│       │   ├── build.mjs      # 构建打包（如有）
│       │   ├── deploy.py      # 自定义部署逻辑（如有）
│       │   └── ...
│       └── tests/             # 测试（不部署）
├── project-skills/            # 按名称分发到任意目标项目
│   └── <skill-name>/
│       ├── SKILL.md
│       └── scripts/
└── AGENTS.md                  # 项目开发规则
```

## 安装原理

### Skills

1. **依赖下载**：仅当技能包含 `package.json` 时，在仓库根执行 `pnpm install --ignore-scripts`；纯工作流技能无需安装运行时依赖。computer-use 的 Python 运行时由 `--tools` 单独管理，并要求锁文件哈希与适配指纹同时通过。
2. **部署全局技能**：将 `skills/` 中选中的技能复制到全局目录。
3. **部署项目级技能**：仅在同时提供 `--project-skills --project-dir <目录> --name <技能>` 时，将 `project-skills/<技能>/` 复制到目标项目的 `.agents/skills/<技能>/`。
4. **隔离作用域**：默认安装不会扫描或安装 `project-skills/`。

### Context

`context/` 目录下的 `.md` 文件会被复制到 `~/.pi/agent/`。命名为 `context` 而非 `prompts`，是为了与 Pi 自身的 `~/.pi/agent/prompts/`（提示词模板 / 斜杠命令）区分。目前支持：

| 文件 | 作用 |
|---|---|
| `APPEND_SYSTEM.md` | 追加到 Pi 系统提示词末尾，定义全局行为规则 |

### Extensions

`extensions/` 下的文件和目录会被复制到 `~/.pi/agent/extensions/`，CLI（`pi`）与 GUI（pi-agent-chat）共享同一目录，改动对两边同时生效。

| 扩展 | 作用 |
|---|---|
| `notify.ts` | 任务完成时发送 Windows toast 通知 |
| `enable-core-tools.ts` | 每次 `session_start` 时把 SDK 内置的 `grep` / `find` / `ls` 补进激活集（Pi 默认只激活 `read`/`bash`/`edit`/`write`） |
| `tools-status.ts` | 提供 `/tools-status` 斜杠命令，统计本会话**每个工具**的调用 / 完成 / 出错 / 未完成次数，并附带思考段数等辅助指标 |

`/tools-status` 无参数，一次输出整份报告（范围固定为当前分支，首行为标题行）：

```
工具使用统计（当前分支）
工具调用 106 次 / 5 个工具 · 完成 106 · 未完成 0 · 出错 4（3.8%）
bash: 调用 49 · 完成 49 · 出错 2
read: 调用 29 · 完成 29
edit: 调用 19 · 完成 19 · 出错 2
…
思考 60 段 / 60 轮 · 47749 字符
模型 anthropic/claude · 思考等级 medium
```

口径：工具调用按 assistant 消息里的 `toolCall` 块计，结果按 `toolResult` 消息计，未完成 = 调用 − 结果（Esc 中断 / 崩溃）；用户 `!` 命令（`bashExecution`）不计入工具调用；被扩展拦截的调用与真实失败一样记为 `isError`；subagent 子会话独立记账，父会话里只算 1 次工具调用。

输出通道：一次 `ctx.ui.notify(整份报告)`，CLI 与 GUI 共用同一条路径，效果一致——报告都留在对话流里而不是弹层：TUI 的 `notify(info)` 向聊天区追加一段文本，pi-agent-chat 把扩展的 `notify` 渲染成 transcript 卡片（首行作标题，多行自动折叠）。无 UI 的 `json` / `print` 模式下 `notify` 是 no-op。

口径：思考段数按 `thinking` 块计、思考轮数按含 thinking 的 assistant 消息计；用户 `!` 命令（`bashExecution`）不计入工具调用；被扩展拦截的工具调用与真实失败一样记为 `isError`；subagent 子会话独立记账，父会话里只算 1 次工具调用。详见 `SESSION-STATS-PLAN.md` §2。

`grep` 依赖 `rg`、`find` 依赖 `fd`，二进制统一放在 `~/.pi/agent/bin/`（与 Pi SDK 同一位置，CLI 与 GUI 共用）。安装脚本的处理规则：

| 情况 | 行为 |
|---|---|
| `bin/` 里没有 | 从 GitHub Releases 下载最新版 |
| 下载失败 | **中断安装**，打印资产 URL 和目标路径，手动放好后重跑即可 |
| `bin/` 里已有 | 对比 GitHub 最新版；有新版时询问 `[y/N]`，输入 `y` 才升级（`--force` 不会绕过询问，非交互环境只提示不升级） |
| 升级失败 | 保留原版本，不中断安装 |
| 仅系统 PATH 上有 | 只报告版本，不接管升级 |
| `PI_OFFLINE=1` | 跳过下载和更新检查并提示 |

静态测试：

```shell
# 扩展逻辑
node --experimental-strip-types --test tests/enable-core-tools.test.mjs tests/tools-status.test.mjs

# install.py 的二进制检查/下载/升级逻辑
python -m unittest discover -s tests -p 'test_*.py'
```

开发阶段的 `node_modules/`、`dist/`、lock 文件均不纳入 Git。

### Local web search

local-web-search 是通用工作流，不绑定浏览器技能名或 Agent，也不再自带浏览器驱动和子 Agent 启动器。默认使用用户已授权的本机有界面个人 Chrome Profile，不主动启动或切换到 headless、隔离 Profile 或云端浏览器，默认搜索 Bing 国际版；公开搜索无需账号或 API key，受限资源由用户在当前 Profile 手动登录后继续读取。

默认上限为 3 次搜索、累计 30 个内容页面、链接深度 3 层；具体计数及安全边界见 [SKILL.md](skills/local-web-search/SKILL.md)。没有可用的本地浏览器能力时会说明限制，不自动转向云端或安装新工具。

覆盖旧版时须移除遗留脚本；统一安装入口的 `--force` 会替换目标技能目录，无需执行已移除的技能内部署脚本。

### browser-use

browser-use 是本仓库的个人 Chrome 技能，不是同名的 browser-use Python Agent 框架；它依赖的上游工具包和 CLI 仍是 browser-harness。

仅通过 `python '<skill-dir>/scripts/chrome.py'` 连接 Windows 稳定版、有界面的个人 Chrome，不以原生官方 CLI 作为本地连接入口。每任务使用独立 `--session`，复用已授权连接；`--stop` 不关闭用户 Chrome 或其他会话。

`sync` 继续通过 uv 安装/升级官方 browser-harness 包；从 `skill-template.md` + `skill-overrides.json` 生成 `SKILL.md`，原始上游说明保存到 `references/upstream-skill.md`。普通 sync 和 `--tools` 都整体刷新说明及 `scripts/`、`references/`，不要求 `--force`；模板和 overrides 不部署。当前适配仅验证 `0.1.13`，未知版本会提示且入口拒绝调用，须维护者验证并更新适配器，不能绕过检查。

技能身份、目录及生成/部署入口统一使用 `browser-use`；`browser-harness` 专指底层依赖。`%LOCALAPPDATA%/custom-skills/browser-harness` 保存该依赖 daemon 的 session 状态，不是技能安装目录；保持路径稳定以便识别和清理已有连接。

诊断、授权、PowerShell here-string 示例、清理边界和待实测矩阵见 [browser-use README](skills/browser-use/README.md)。

## 本地验证（不安装）

local-web-search 无需构建，在仓库根运行静态契约测试：

```shell
python -m unittest discover -s tests -p 'test_local_web_search.py'
```

浏览器诊断、联网与登录交接的验证方式见 [技能 README](skills/local-web-search/README.md#验证)；测试不会部署到全局目录。

## 添加新技能

1. 全局技能在 `skills/` 下创建目录；必须按名称显式分发到目标项目的技能放在 `project-skills/`。
2. 编写 `SKILL.md`（遵循 Agent Skills 规范）。
3. 如需依赖，添加 `package.json`。
4. 如需自定义部署逻辑，添加 `scripts/deploy.py`。
5. 添加测试到 `tests/`。
6. 全局技能运行 `python scripts/install.py --name <skill-name>` 验证；项目级技能运行 `python scripts/install.py --project-skills --project-dir <project-root> --name <skill-name>` 验证。
