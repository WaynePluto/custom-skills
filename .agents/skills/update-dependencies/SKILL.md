---
name: update-dependencies
description: 检查并更新本仓库（custom-skills）的上游依赖。核心是 browser-use 依赖的 browser-harness 与 computer-use 依赖的 cua-driver / comtypes；两者都无新版本时跳过其余检查。负责同步哈希锁定、兼容列表、代码与文档中的版本号，并运行 pnpm test 验证。当用户要求更新依赖、升级 browser-harness / cua-driver，或检查本仓库上游新版本时使用。不用于安装或部署技能（pnpm sync 已覆盖），也不管理 rg / fd 二进制（install.py --binaries 交互处理）。
---

# update-dependencies

本仓库没有 npm 运行时依赖（根 package.json 无 dependencies，pnpm workspace 当前为空），真正的"依赖"是两个工具型技能锁定的上游包：

| 依赖 | 使用方 | 锁定位置 | 上游 |
|---|---|---|---|
| browser-harness | browser-use | [compatibility.json](../../../skills/browser-use/scripts/compatibility.json) 的 `browser_harness_versions` | [browser-use/browser-harness](https://github.com/browser-use/browser-harness)，PyPI 同名 |
| cua-driver + comtypes | computer-use | [requirements.lock](../../../skills/computer-use/scripts/requirements.lock)（wheel SHA-256）+ [compatibility.json](../../../skills/computer-use/scripts/compatibility.json)（审定元数据） | [trycua/cua](https://github.com/trycua/cua)（tag 形如 `cua-driver-rs-vX.Y.Z`），PyPI 同名 |

版本不变式由仓库测试统一检查，开始更新前先跑基线，不要在技能里另写判定逻辑：

```powershell
pnpm test
```

其中 `tests/test_computer_use_skill.py` 断言 lock 的精确版本与哈希，`tests/test_browser_use_skill.py` 断言维护文档中的适配版本；升级后这些断言与 `skills/computer-use/scripts/driver_bridge.py` 的 `EXPECTED_VERSION` 都要同步。

## 步骤

### 1. 检查核心上游是否有新版本

```powershell
# 已安装 / 已锁定的当前版本
uv tool list                                                        # browser-harness 已安装版本
Select-String -Path skills/computer-use/scripts/requirements.lock -Pattern "=="

# 上游最新版本（PyPI 与 install.py 的安装源一致）
(Invoke-RestMethod https://pypi.org/pypi/browser-harness/json).info.version
(Invoke-RestMethod https://pypi.org/pypi/cua-driver/json).info.version
(Invoke-RestMethod https://pypi.org/pypi/comtypes/json).info.version
```

直连失败时按仓库根 `.env` 的 `GITHUB_PROXY` 配置代理重试。

- 两个核心依赖都无新版本：告知用户"已是最新"，确认基线测试通过后**结束，不再检查其他依赖**。
- 有新版本：按下面对应小节处理；两个技能互不依赖，可分别升级。

comtypes 是只读 UIA 防护辅助依赖，仅在安全修复或 cua-driver 需要时随 cua-driver 一起评估，不单独追新。

### 2. 升级 browser-harness（browser-use）

`sync` 即升级（`uv tool upgrade`，不询问），并整体重新生成 SKILL.md 与 references：

```powershell
python scripts/install.py --tools --name browser-use
```

升级后新版本不在 `browser_harness_versions` 中时，chrome.py 入口会拒绝连接（`incompatible_version`）——这是预期防护，不是要绕过的故障。放行流程：

1. 审阅 `git diff` 中 `skills/browser-use/references/upstream-skill.md`（上游说明原文）与生成的 `SKILL.md`，重点核对 `chrome_session.py` / `chrome_runtime.py` 复用的官方模块、daemon IPC 协议与页面 helpers 是否变化。
2. 离线测试：`python -B -m unittest discover -s tests -p 'test_browser_use_*.py'`，再加 `test_install_browser_use.py`。
3. 真实浏览器冒烟（需用户配合授权）：`chrome.py --doctor` → `--ensure` → 只读页面脚本 → `--stop`；状态矩阵见 [maintenance.md](../../../skills/browser-use/references/maintenance.md)。
4. 验证通过后把新版本号加入 `compatibility.json` 的 `browser_harness_versions`（可保留旧版本）。

### 3. 升级 cua-driver / comtypes（computer-use，审慎）

这是安全审计型升级：lock 锁 wheel SHA-256，compatibility.json 记录审定 tag/commit、源码树哈希与接口签名，`driver_bridge.py` 的 `EXPECTED_VERSION` 也要同步。哈希没算清之前不要改任何文件。

1. 收集上游事实：GitHub tag `cua-driver-rs-vX.Y.Z` 与对应 commit；PyPI 新 wheel 的文件名和 sha256：

   ```powershell
   (Invoke-RestMethod https://pypi.org/pypi/cua-driver/json).releases['<新版本>'] |
     Where-Object filename -like '*win_amd64.whl' | Select-Object filename, digests
   ```

2. 把新 wheel 装进临时 venv，按 `driver_bridge.verify_package` 的口径计算 `file_count` 与 `code_tree_sha256`：

   ```powershell
   uv venv --python 3.13 "$env:TEMP\cua-audit"
   uv pip install --python "$env:TEMP\cua-audit\Scripts\python.exe" 'cua-driver==<新版本>' 'comtypes==<版本>'
   & "$env:TEMP\cua-audit\Scripts\python.exe" -I -c 'import hashlib, importlib.metadata as im
   from pathlib import Path
   for name, module in (("cua-driver", "cua_driver"), ("comtypes", "comtypes")):
       root = Path(im.distribution(name).locate_file(module))
       files = sorted((p for p in root.rglob("*") if p.suffix in {".py", ".dll", ".exe"}
                       and "gen" not in p.relative_to(root).parts),
                      key=lambda p: p.relative_to(root).as_posix())
       d = hashlib.sha256()
       for p in files:
           d.update(p.relative_to(root).as_posix().encode("utf-8")); d.update(b"\0")
           d.update(p.read_bytes()); d.update(b"\0")
       print(name, len(files), d.hexdigest())'
   ```

3. 依次更新：`requirements.lock`（版本 + `--hash=sha256:`）、`compatibility.json`（`backend.version`、`audited_tag`、`audited_commit`、`packages[]` 的 `version` / `artifact` / `wheel_sha256` / `file_count` / `code_tree_sha256`；接口有变化时同步 `interfaces` 并适配调用层）、`driver_bridge.py` 的 `EXPECTED_VERSION`。审计完删除临时 venv。
4. 重装运行时（staging 新环境 → `--require-hashes` 与哈希/接口验证 → 原子切换，任一步失败保留原环境）：

   ```powershell
   python scripts/install.py --tools --name computer-use
   ```

5. 离线测试 `python -B -m unittest discover -s tests -p '*computer_use*.py'`；真实验收（用户配合 reload）按 [maintenance.md](../../../skills/computer-use/references/maintenance.md)：doctor → windows → snapshot → 单次动作 → 独立验证 → cleanup。

任何一步验证失败：恢复原锁定文件，向用户报告差异，由用户决定是否继续。

### 4. 同步文档与测试中的版本号（必做）

升级前记下旧版本号，升级后全文搜索逐一核对：

```powershell
rg -n "<旧版本号>" README.md skills/ tests/
```

已知承载位置（判定口径：只更新描述"当前版本"的陈述，版本相对行为的注释与历史验证记录保持原样）：

- `README.md`：「当前适配仅验证 X」段。
- `skills/browser-use/references/maintenance.md`：「已验证适配版本 X」。
- `skills/computer-use/references/upstream.md`（发布、wheel 文件名、comtypes 版本）、`maintenance.md`（架构图与依赖清单）、`workflow.md`（「SDK X 的 UIA 树…」）。
- `tests/` 中把当前版本当 fixture 的断言（`test_browser_use_skill.py`、`test_computer_use_skill.py`、`test_install_browser_use.py`、`test_install_computer_use.py`）。

### 5. 验证（必做）

```powershell
pnpm test    # Node + Python 全部静态测试
```

再按所升技能执行对应真实冒烟（见上文各节）。全部通过才算升级成功；未执行的真实验证要在汇报中明确说明。

### 6. 其他依赖面（仅当核心依赖有更新时顺带检查）

- rg / fd：`python scripts/install.py --binaries` 交互确认升级；仓库内无版本锁定，无需同步文档。
- npm workspace：根 package.json 与各技能 package.json 出现 `dependencies` 时才有意义，届时 `pnpm outdated`；当前为空属正常，不要为升级而引入依赖。
- pixel2ase（numpy / Pillow）：设计上不在仓库内锁定，由目标项目环境按需安装，不随本技能升级。
- uv / Node / pnpm / PowerShell 7：宿主工具链，sync 已按需自动安装 uv，其余不在本技能范围。

## 完成后

向用户汇报：各依赖旧 → 新版本、更新了哪些锁定 / 兼容 / 文档 / 测试文件、`pnpm test` 与真实验证结果、未升级项及原因（如 comtypes 无需跟随）。
