# browser-use 维护说明：适配、同步与验证

面向维护者的实现与验证资料；Agent 任务工作流以 [SKILL.md](../SKILL.md) 和 [page-workflow.md](page-workflow.md) 为准。

## 适配与同步维护

入口以 `-I` 启动 uv 工具环境中的 Python，在隔离进程内固定官方 daemon 的 Chrome 端点并复用官方 CLI/helpers；**不修改第三方 `site-packages`**。

除连接选择与启动调度外，`scripts/chrome_session.py` 定义只读专用页查询/校验，`scripts/chrome_runtime.py` 为认证 IPC 增加该协议，并只在本次官方 `run` 模块的脚本 globals 中注入 `session_tab()`，不替换官方页面 helpers。查询前宿主核验 PID、创建时间、generation、binding 和 token，响应也核对会话身份；查询使用 `Target.getTargetInfo`，页面命令固定显式 session，两者均避免进入上游隐式 stale-session 恢复。

底层 browser-harness daemon 的 session IPC、日志和状态放在 `%LOCALAPPDATA%/custom-skills/browser-harness/<session>/`；这是依赖运行目录，不是技能安装目录，保持稳定以便识别和清理已有 session。复用/停止前同时验证 daemon 的 PID、创建时间、会话代次和端点绑定，不用旧状态接管新进程；本机连接固定直连，不经继承的代理。

[`scripts/compatibility.json`](../scripts/compatibility.json) 当前只列出已验证适配版本 **0.1.13**，不代表上游以后版本无条件兼容。`sync` 仍按原语义执行 `uv tool upgrade browser-harness`，不会为了适配而静默锁住升级；遇到未知版本会提示，入口报 `incompatible_version` 并拒绝调用。必须由维护者核对上游内部 API、完成回归与真实浏览器验证后更新适配器及兼容列表；不能只为消除错误随意加版本、删检查或改用裸 CLI。

维护安装时的行为（不是页面任务运行时行为）：

| 情况 | `sync` 行为 |
|---|---|
| 缺少 uv | 提示后跳过本段，不中断其他部署 |
| 尚未安装官方包 | `uv tool install --python 3.12 browser-harness`，由 uv 管理 Python 环境 |
| 已安装 | `uv tool upgrade browser-harness`，不询问；升级失败尝试用现有版本继续生成 |
| `PI_OFFLINE=1` | 未安装则跳过；已安装则不升级，仍用现有版本生成并检查兼容性 |
| 上游说明或模板生成失败 | 跳过本段，不把裸 CLI 工作流部署为本地入口 |

生成与部署链路：

1. `browser-harness skill` 的原始输出保存到 [upstream-skill.md](upstream-skill.md)，不改写其内容。
2. 仓库的 `skill-template.md` 提供本地 Chrome 工作流，`skill-overrides.json` 提供审定的 frontmatter，合成仓库 `SKILL.md`；仓库副本进 Git，便于审查变化，不直接手改生成文件。
3. **普通 `pnpm sync` 和 `python scripts/install.py --tools` 均整体刷新**目标技能的说明、`scripts/` 和 `references/`，删除旧部署残留，不是仅 `--force` 才刷新。`--skills` 单独执行不安装/升级工具或重新生成入口。
4. `skill-template.md`、`skill-overrides.json` 是仓库同步元数据，不部署给 Agent；`scripts/compatibility.json` 随运行脚本部署。不要在全局技能目录修改适配器作为仓库修复。

## 验证与真实状态矩阵

仓库根目录运行离线静态测试（mock 浏览器/子进程，不启动真实浏览器，不执行全局 sync）：

```powershell
python -B -m unittest discover -s tests -p 'test_browser_use_*.py'
python -B -m unittest discover -s tests -p 'test_install_browser_use.py'
```

工作流测试校验生成一致性、文档契约和示例在 mock helpers 下的控制流，不证明模型实际遵循规则或真实网页交互已通过。安装测试只操作临时目录，验证技能身份与依赖名分离、生成一致性和完整部署刷新。

真实验收须由用户安排浏览器状态并完成授权，按 `--doctor` → `--ensure` → 只读页面脚本 → `--stop` 检查；不以 mock 测试代替真实矩阵。

本次专用页复用验证：从仓库入口启动 `dedicated-smoke`，用户允许调试后，`session_tab()` 返回新 daemon 的唯一专用 `about:blank`。同一脚本记录页面 target ID 集合，在该 ID 上导航 GitHub browser-harness 仓库；加载成功，导航前后集合相同、专用 ID 不变，证明没有为首导航额外创建第二个页面。紧邻的第二次 CLI 调用再次读取同一非空专用页，URL、ID 与 target 集合均不变，没有覆盖或新建。首次验收尝试中该页曾在调用间消失，helper 返回结构化 `session_tab_unavailable`，浏览器级只读检查确认原 ID 不存在且未自动恢复；重新授权后的复验稳定通过。最终 `--stop` 返回 `stopped`，随后 `doctor` 为 `daemon_idle`，Chrome 端点仍可验证。未全局部署，未验证用户主动关闭后继续写命令、真实点击/输入、并行第二页和登录交接。

历史验证（技能改名时，早于本次专用页复用优化）：从仓库 `skills/browser-use/scripts/chrome.py` 执行 `doctor` → `ensure`，状态由 `daemon_idle` 到 `ready`；无参新建任务页并访问 GitHub 官方 browser-harness 仓库，显式检查加载结果及实际 URL，再跨调用核验同一 ID、读取过滤后的 AX 节点。关闭动作后首次列表仍有短暂残留，只读复查确认消失，没有重复关闭；其他标签 ID 在关闭前后保留。最后 `--stop` 返回 `stopped`，诊断显示 Chrome 端点仍可验证且本 session daemon 不在运行。未全局部署、未更改安全设置，也未验证真实点击、输入、弹出页、登录交接或完整多浏览器矩阵。

历史验证（技能改名前）：Edge 保留 8 个后台进程时自动打开 1 个 Chrome 窗口；用户授权后，多次调用复用同一连接，读取公开页面的标题及搜索框。`example.com` 返回 `net::ERR_CONNECTION_RESET`；Bing 首页重定向到 `https://cn.bing.com/`，实际读取成功，但不代表国际版搜索验证通过。最终复验返回 `ready` → `completed` → `stopped`，本 session 的 PID/端口/状态文件已清理，Chrome 窗口和 Edge 后台进程均保留。未修改用户浏览器安全设置。

| Chrome 状态 | Edge 状态 | 预期行为 | 历史真实验证 |
|---|---|---|---|
| 关闭 | 关闭 | 诊断不启动；ensure 正常启动个人 Chrome | 未执行 |
| 关闭 | 打开窗口 | 只启动 Chrome，不连接或关闭 Edge | 未执行 |
| 关闭 | 仅后台进程 | Edge 后台不阻止 Chrome 启动 | 已验证，Chrome 窗口打开，Edge 进程保留 |
| 已打开 | 关闭 | 复用 Chrome，不重启 | 未执行 |
| 已打开 | 打开窗口 | 只连接所选 Chrome，Edge 不受影响 | 未执行 |
| 已打开 | 仅后台进程 | 仍复用 Chrome，不切换目标 | 已验证多次页面调用复用连接 |

还须分别验证：inspect 未授权、授权弹窗允许/拒绝/180 秒超时、同 session 连续调用不重复弹窗、两个 session 互不停止、端口 owner 或进程创建时间变化时拒绝复用、脚本超时与输出截断、用户关闭 Chrome 后明确报错。专用页复用还须验证：用户主动关闭后的页面写命令不会重放、并行第二页不会混淆归属、交接不克隆。收尾核对用户原有标签和 Chrome 仍在、脚本额外标签已清理。未知上游版本的拒绝行为可用离线测试验证，不靠绕过检查做真实冒烟。
