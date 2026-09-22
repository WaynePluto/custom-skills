# cua-driver 上游基线与适配范围

## 固定发布基线

- 仓库：[trycua/cua](https://github.com/trycua/cua)
- 发布：`cua-driver 0.28.2`，tag `cua-driver-rs-v0.28.2`
- 源码 commit：`fc188250b4ca8549b8e61f937fdb1fb560770e86`
- 包：`cua_driver-0.28.2-py3-none-win_amd64.whl`
- wheel SHA-256：`b987cfb1ac3eb7c682650f8cdfd3698ca3d31adeb0b0cf419829025ca4aa2be8`
- 辅助依赖：`comtypes 1.4.17`，仅补查 UIA 密码属性和控件身份，不实现输入。
- 对照参考：Hermes Agent `v2026.9.21` 的 `tools/computer_use/cua_backend*.py`。本仓库不导入或复制其实现，不采用其自动修复、daemon、审批绕过或前台升级流程。

包及原生文件哈希见 [compatibility.json](../scripts/compatibility.json)，可安装 artifact 见 [requirements.lock](../scripts/requirements.lock)。同版本但代码/DLL 改动也拒绝；这些哈希不证明 DLL 可以从对应 commit 可复现构建。

## 官方 SDK，不使用 MCP 包装

`CuaDriver.create()` 在当前 worker 进程中创建 Rust runtime；SDK 通过 UniFFI 绑定调用，异步方法由一个本地事件循环执行。`shutdown()` 等待已准入操作结束。不存在跨 CLI 调用存活的 SDK、COM 对象或上游 token。

使用 typed `list_windows`、`get_window_state` 和 `click`；其它动作通过内部受限 `call_tool` 适配，以便明确传递 HWND、PID、token 和 `delivery_mode`。Agent 没有通用工具调用入口。

`RuntimeOptions::embedded()` 默认关闭 cursor overlay，direct runtime 不安装 daemon history。适配层额外固定 `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` / `CUA_TELEMETRY_ENABLED=0`，清除其它继承的 CUA 配置；不调用授权升级、浏览器、剪贴板、录制或远程传输接口。

## 已审计路径

以下路径均位于上述固定 commit 的 `libs/cua-driver/` 下：

| 路径 | 关注点 |
|---|---|
| `python/src/cua_driver/__init__.py` | Python SDK 导出和同进程创建入口；实际 artifact 的目录布局以 wheel 为准 |
| `rust/crates/cua-driver-sdk/src/lib.rs` | SDK 方法、typed click、错误和 shutdown |
| `rust/crates/cua-driver-sdk/src/runtime.rs` | embedded 默认关闭 overlay、Windows 交互桌面检查、生命周期 |
| `rust/crates/platform-windows/src/tools/impl_.rs` | 窗口树、窗口截图、元素 click、type、set_value、scroll、drag |
| `rust/crates/platform-windows/src/input/delivery.rs` | background/foreground 路由及框架限制 |
| `rust/crates/platform-windows/src/input/mouse.rs` | Chromium 单次左键 PostMessage、物理输入的差异 |
| `rust/crates/platform-windows/src/uia/mod.rs` | 元素 actions 和边界；当前 structured API 不含 IsPassword |
| `rust/crates/platform-windows/src/uia/fg_bypass.rs` | 抑制自激活期间可能临时禁用目标窗口 |

## 不能只信任 background 标志

0.28.2 源码中 MSAA 元素点击在 background 下仍可能调用 `send_click_synthesized_mods`。WPF/GTK 等回退路径还可能使用坐标/触摸注入。因此薄适配层额外限制窗口类、动作形态和目标方式；未审定路径在调用前拒绝。

Chromium 单次左键元素动作有独立 PostMessage 分支，不经过一般 UIA/注入回退。但 `post_click_on()` 在目标自行成为前台时会调用 `force_foreground_attached(prev_fg)` 恢复原前台；不能承诺绝对无焦点切换，也不能用前后相同采样证明期间没有切换。此结论来自固定版本源码，不等于 Chrome 授权气泡已通过实测。

## 观察与引用限制

- UIA 元素 `frame` 是物理屏幕坐标；前台元素滚动/拖拽按固定 SDK 的 DWM 左上角内缩一像素逆变换。该 worker 不采集图片，不存在截图缩放缓存。
- `get_window_state` 必须显式 `include_screenshot=false` 才是纯 UIA 观察；截图按需独立调用。
- 返回 `elements_complete=false` 不能证明元素消失。
- 上游 token 受 runtime/快照生命周期约束。本地持久化仅保存事实，动作 worker 重新采样后唯一重定位，并使用新的 token。
- `WindowElement` 不提供密码状态，因此不输出 value；文本动作前用只读 COM 查询 `CurrentIsPassword`，未知即拒绝。
- Windows `scroll` 的后台元素参数不能保证滚到所选嵌套控件，因此本技能未开放后台 scroll。
- 窗口 `move_cursor` 只是 overlay 动画，不能保持旧 CLI 的真实鼠标语义，明确拒绝。
- typed 窗口截图缺少实际裁剪原点；不能把 window_bounds 冒充图像原点。图片返回 `coordinates_usable=false`，仅作视觉验证，拒绝像素输入。
- 不自动把 window capture 不足升级为 desktop capture；Chrome 授权气泡可能需要独立 HWND。

## 许可与升级

cua-driver wheel 和 comtypes 使用 MIT；未复制 Hermes 或驱动代码。重新分发上游二进制时应保留 wheel 中的许可证并审核原生传递依赖许可。

升级必须同时审核 wheel、绑定与 native 库、更新哈希和接口清单，跑离线测试及用户监督的桌面矩阵；不能仅放宽版本范围。运行期始终不检查最新版、不安装依赖。
