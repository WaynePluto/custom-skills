# 命令与恢复工作流

所有命令均通过 `python '<skill-dir>/scripts/computer.py'`。Windows 使用 PowerShell 7。

## 公共参数与结果

- `--session`：1–40 个英文字母、数字、下划线或连字符；除 doctor 外必填。
- `--request-id`：动作必须提供全新规范 UUID；同 ID 同参数只返回旧结果，不再次 dispatch。
- `--timeout`：1–120 秒，默认 30 秒，覆盖启动、输入正文、SDK 执行和退出。
- 动作参数 `--delivery background|foreground`：默认 background；没有自动降级模式。
- stdout 是不超过 24 KiB 的单个 JSON；完整对象裁剪，不截成无效 JSON。`truncated=true` 表示结果不完整。
- `side_effect`：`none`（未下发）、`dispatched`（已下发）、`uncertain`（可能已发生）。
- `verified` 只针对明确后置条件；普通输入始终需要独立业务验证。`driver_verdict` 仅是上游证据。

## 观察命令

| 命令 | 说明 |
|---|---|
| `doctor` | 校验锁定版本、绑定/DLL 哈希、SDK 创建/关闭和交互桌面；不截图或输入 |
| `windows` | SDK 列窗 + 只读 Win32 核验，最多返回 300 个可确认身份的可见窗口 |
| `snapshot --window REF [--max-elements 200]` | HWND 定向 UIA 观察，不聚焦、不截图；最大 500 个节点 |
| `screenshot --window REF` | 显式窗口 PNG，最长边不超过 1920；返回文件和快照引用 |
| `wait --window REF --condition appears|disappears|text|foreground` | 有界只读条件验证，不隐式聚焦 |
| `cleanup` | 清理当前 session，不关闭应用，不回滚动作 |

`wait` 元素条件使用精确 `--name`，可附 `--control-type`。文本条件附 `--text`，会先只读检查该编辑控件不是密码字段；匹配值不回显。`--wait-seconds` 默认 10，必须给整体 timeout 留足余量。

SDK 0.28.2 的 Windows UIA 树不能证明完整遍历，通常 `tree_complete=false`。因此 `disappears` 不能把“没看到”当成“已消失”，可能一直返回 `verification_failed`。

`--display` 为兼容旧 CLI 仍可解析，但返回 `unsupported_action`，不转成主显示器截图。目标最小化时不恢复。

## 默认后台动作

```powershell
python '<skill-dir>/scripts/computer.py' --session task1 --request-id '<uuid>' click --snapshot '<snapshot-ref>' --element '<element-ref>'
```

- 当前只准入元素单次左键；窗口类限定 Chromium/CEF、WinForms、经典 `#32770`/Notepad、WinUI3/ApplicationFrameWindow。
- SAL/MSAA、WPF、GTK、未知类或其它点击形态在 dispatch 前拒绝，避免上游潜在 SendInput/触摸注入路径。
- 上游已接受请求后，即便返回“后台不可用”，也不把它当成可安全重试的纯拒绝。
- 被其他窗口遮挡不直接阻止后台元素动作；仍严格检查窗口身份、布局、DPI、唯一元素事实及同进程新 token。

后台文字仅支持 `type --snapshot REF --element REF --clear`，使用 SDK 的 `set_value` 替换整个非密码编辑控件。

**PowerShell 字符串管道会附加换行，而 CLI 拒绝所有控制字符。** 输入应通过能准确写出 UTF-8 字节且不附换行的宿主 stdin 功能（或固定命令的 `ProcessStartInfo.RedirectStandardInput` + `StandardInput.Write`）。正文不能放进 argv，也不要通过屏幕内容构造输入脚本。

单次最多 400 字符；拒绝密码/MFA/验证码/令牌。`--press-enter` 不执行组合提交，必须输入后重新观察再独立 Enter。

## 显式前台动作

仅在用户确认允许影响共享鼠标/键盘后使用；用户并行工作期间不得自行升级。

```powershell
python '<skill-dir>/scripts/computer.py' --session task1 --request-id '<uuid>' focus --window '<window-ref>' --delivery foreground
python '<skill-dir>/scripts/computer.py' --session task1 --request-id '<uuid>' click --snapshot '<new-snapshot-ref>' --element '<element-ref>' --delivery foreground
```

`focus` 返回新快照。输入之前再次确认目标仍处于前台；不自动重新 focus。

| 动作 | 参数与限制 |
|---|---|
| `click` | `--snapshot` + `--element`；可选 `--button left|right|middle`、`--clicks 1|2` |
| `type` | 仅元素，正文 stdin；无 `--clear` 时使用定向 type_text，`--clear` 为语义值替换 |
| `shortcut` | `--snapshot --window --keys`，精确组合白名单；Enter 应单独执行 |
| `scroll` | `--snapshot` + 元素，`--direction up|down|left|right`、`--amount 1..20` |
| `drag` | `--snapshot`、`--from-element`、`--to-element`，`--duration 0..10` |
| `move` | 拒绝，不把 overlay 移动伪装为真实指针移动 |

旧 `--point X Y --space image --window REF` 参数仍可解析，但当前返回 `unsupported_action`：SDK typed 截图没有可验证的实际裁剪原点，图片仅用于视觉验证，不能把窗口边界当成图片原点。前台元素滚动/拖拽使用当前 UIA 物理坐标和固定 SDK 的 DWM 位图原点换算，并核验最上层命中窗口。负原点及非主显示器不被硬编码排除，但多屏/DPI 能力须实测。

## 引用生命周期

- `window_ref` 绑定 HWND、PID、进程创建时间、exe、窗口类和标题。
- `element_ref` 只属于本地当前快照；不是 SDK 列表下标的永久别名。
- 每个 worker 自建 SDK；动作前重新观察同一 HWND，按名称、类型、父路径和边界唯一重定位，再使用该 worker 的新 token。
- 不持久化或复用上游 token；窗口布局/DPI、显示器拓扑或动作代次变化后重新观察。
- 快照有效期 120 秒，动作提交时原子消费；共享桌面动作代次使其它 session 的旧快照也失效。dispatch 意图和全局代次均持久化成功后才调用 SDK。
- 旧驱动/版本的 session 返回 `invalid_state`；先 cleanup 再重新列窗，不迁移旧引用。

## 不确定结果与清理

- `uncertain`、`driver_refused`、`interference_detected`、超时或崩溃后只读观察，不能重放。
- 后台前后焦点/鼠标变化会保守暂停，也可能只是用户在操作；适配层不恢复用户光标或焦点，不能由两次采样推断中间从未变化。
- session 最多 20 张图片、累计 50 MiB，单张 10 MiB。cleanup 只删除该 session；过期状态在后续调用中回收。
- 安装/升级由维护者运行仓库 sync；执行期不联网修复依赖。
