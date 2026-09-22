# computer-use：基于 cua-driver 的 Windows 桌面 CLI

Agent 入口见 [SKILL.md](SKILL.md)，完整命令见 [workflow.md](references/workflow.md)，限制见 [safety.md](references/safety.md)，源码依据见 [upstream.md](references/upstream.md)。

## 架构

```text
Agent → computer.py（CLI、stdin、有界 JSON、Job Object 监督）
      → runtime.py + state.py + guard.py（锁、一次性快照、去重、验证）
      → backend.py + driver_bridge.py（cua-driver SDK 薄适配）
      → cua-driver 0.28.2 同进程 Rust runtime
      → 当前 Windows 交互桌面
```

`win32_guard.py` 仅做只读进程/窗口身份、鼠标/焦点、显示器和非密码控件检查；所有 GUI 写动作均交给 cua-driver。不启动 MCP Server、HTTP 服务、常驻 daemon 或完整 Agent 框架。

- 一次 CLI 一个受监督 worker，同一 worker 内持有 SDK 及 native token；退出前 `shutdown`。
- 保留原有请求 UUID、dispatch 意图持久化、一次性快照、超时 uncertain、状态配额及安装事务。
- 默认后台限定：不支持时拒绝，不自动降级为 foreground。前台动作必须显式指定执行方式并取得用户允许。
- 技能只用于本机 GUI，不开放浏览器 DOM、Shell、Registry、文件系统、进程终止或任意程序启动。

## 独立运行时与安装

Windows 11 x64，PowerShell 7，uv 管理的独立 CPython 3.13 venv。依赖仅 `cua-driver==0.28.2`、只读 UIA 防护用 `comtypes==1.4.17`，都锁定 wheel SHA-256。SDK wheel 附带 DLL 和 CLI，但本技能不执行后者。

```powershell
python scripts/install.py --tools --name computer-use
```

除非用户明确要求，不部署全局目录。安装先创建新环境，执行 `--require-hashes`、源码/原生文件哈希及接口验证，再原子切换；任一步失败保留原运行时和技能。成功后删除旧备份，不保留另一套驱动作为后备。

仅复制技能而不安装依赖：

```powershell
python scripts/install.py --skills --name computer-use
```

离线时设置 `PI_OFFLINE=1`，只复用通过验证的现有环境；不解析远端最新版、不下载 Python/wheel、不覆盖未知环境。任务运行期不联网、不自动安装或升级。

## 后台能力与兼容性变化

- 后台观察不再要求前台；UIA 与截图分开，默认不截图。
- 后台动作仅准入已审核窗口类的元素单次左键及 `type --clear` 语义替换。Chrome 点击使用 SDK PostMessage 路径，仍须验证授权等实际效果。
- 背景像素/右键/双击、快捷键、滚动、拖拽不在本适配层审定范围，明确拒绝。用户允许后可另选 foreground；不存在自动重试或升级。
- `type --press-enter` 不再组合执行；输入后重新观察，再独立提交。不能确认非密码控件时拒绝，默认不输出控件值。
- `move`、`screenshot --display` 及图片坐标输入返回 `unsupported_action`；SDK 截图未暴露实际裁剪原点，不能伪造坐标映射。前台元素点击、滚动和拖拽仍有独立路径。
- 旧后端 session 不复用，需 cleanup 后重新观察。
- SDK 返回 `ok`、`confirmed` 不直接提升为业务 `verified=true`；派发后的任何失败均保守视为可能已有副作用。

## 测试与真实验收

离线测试不创建 SDK、不操作真实桌面、不执行全局 sync：

```powershell
python -B -m unittest discover -s tests -p '*computer_use*.py'
pnpm test
```

覆盖版本/原生文件篡改、前后台分流、密码检查、旧 session、token 不跨 worker、单次快照、去重、失焦、遮挡、超时/uncertain、安装事务和离线模式。

真实诊断通过公开 `doctor`。受控实测在用户 reload 后进行，按 `doctor → windows → snapshot → 单次动作 → 独立验证 → cleanup`。Chrome 测试需要用户明确允许远程调试授权；以 browser-use 连接进入 ready 为后置条件。没有实测的 DPI、多屏、窗口类和输入路径不能宣称已支持。

## 风险与限制

- 后台不是隔离桌面或零干扰保证；上游可能暂时改变窗口属性或禁用窗口，Chromium PostMessage 在目标自行激活时还会内部恢复原前台。要求绝对不触碰焦点时不能依赖此路径。
- 用户应避免同时操作同一目标控件。前后鼠标/焦点变化会停止，但两次采样不能证明中间完全没变化。
- native/UIA 超时可能已有副作用，Job Object 能回收 worker，不能保证撤销目标应用处理或窗口属性变化。
- 不处理 UAC、安全桌面、锁屏、自动提权、密码/MFA/验证码。
- Windows UIA 完整性未知，不能用未观察到的元素证明消失。
- Chrome 独立授权气泡可能不属于所选 HWND；重新列窗定位，不能静默扩大到全桌面。
- 不写剪贴板、不启用录制/浏览器桥接/遥测；状态和图片仅存本机临时目录。

## 许可

直接依赖上游包，不复制 Hermes 或驱动源码。cua-driver wheel 标注 MIT，comtypes 标注 MIT；原生二进制的传递组件另受各自许可约束。本仓库第一方许可仍需维护者明确，不能由依赖许可推定。
