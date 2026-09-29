---
name: git-commit
description: 创建符合规范的 Git commit，遵循 Conventional Commits 格式和项目约定
---

请按照以下规范创建 Git commit：

## Commit 格式规范

### 基本格式

```
<type>: <subject>

<body>

<footer>
```

### Type 类型

- **feat** - 新功能
- **fix** - 修复 bug
- **refactor** - 重构代码（既不是新功能也不是修复）
- **style** - 代码格式调整（不影响代码逻辑）
- **docs** - 文档更新
- **test** - 测试相关
- **chore** - 构建工具、辅助工具的变动
- **wip** - 进行中的工作（Work In Progress）
- **perf** - 性能优化

### Subject 主题

- 使用中文描述
- 简洁明了，不超过 50 字
- 不以句号结尾
- 使用祈使句语气（如"添加"、"修复"而非"添加了"、"已修复"）

### Body 正文

- 详细描述本次提交的内容
- 可以分多条列举
- 每行不超过 72 字符
- 解释 **what** 和 **why**，而不是 **how**

### Footer 脚注

- 关联 Issue：`#123`
- Breaking Changes：`BREAKING CHANGE: 详细说明`

## 常用模板

### 新功能

```
feat: 添加用户登录功能

- 新增登录界面
- 实现表单验证
- 集成后端 API
```

### Bug 修复

```
fix: 修复卡牌拖动延迟问题

将长按触发时长从 200ms 降低到 50ms，提升响应速度
```

### 重构

```
refactor: 重构场景管理器

- 将 PauseScene 改为游戏场景内的弹窗
- 新增 PauseManager 管理暂停弹窗
- 删除不必要的场景切换逻辑
```

### 代码格式

```
style: 删除调试日志

清理项目中的 console.log 调试语句，保留错误和警告日志
```

## Commit 步骤

1. **查看改动**

   ```shell
   git status
   git diff
   ```

2. **一次性提交全部改动**

   每次提交必须把本地全部改动一次性提交到一个 commit 中，**不拆分 commit**。

   无论改动涉及多少个文件、包含多少种不同的目的（重构 + 新功能 + 配置调整 + bug 修复），都合并为单一 commit。

   ```shell
   git add -A
   git commit -m "<type>: <subject>"
   ```

3. **确认 commit**
   ```shell
   git log --oneline -5
   ```

4. **创建 commit**

   **PowerShell 7（pi 默认 Shell）**：多个 `-m` 拼接，每个 `-m` 为一段，git 自动用空行分隔；body 用内联 here-string 实现多行：

   ```shell
   git commit -m "feat: 添加功能描述" -m @"
   - 详细说明 1
   - 详细说明 2
   "@
   ```

   也可把整段 message 放进一个 here-string 变量：

   ```shell
   $msg = @"
   feat: 添加功能描述

   - 详细说明 1
   - 详细说明 2
   "@
   git commit -m $msg
   ```

   > 注意：pwsh here-string 的结束标记 `"@` 必须顶格写在行首，前面不能有任何空格。
   > 不要使用 bash 的 HEREDOC（`$(cat <<'EOF' ... EOF)`）：pwsh 不支持 `<<`，会报 `ParserError: Missing file specification after redirection operator`。
   > 扩展 here-string `@"..."@` 会解释反引号和 `$`：body 中若含行内代码 `\`xxx`\`` 或 `$variable` 会被吞掉或替换。需保留字面量时改用字面 here-string `@'...'@`（不展开任何内容），或在 body 中避免使用反引号。

5. **确认 commit**
   ```shell
   git log -1 --pretty=fuller
   ```

## 推送后检查 GitHub Actions CI

当提交会推送到 GitHub（remote 指向 github.com）且仓库配置了 Actions CI（`.github/workflows/` 下存在工作流文件）时，推送本身不算完成，还必须确认 CI 通过：

1. **推送并查询运行状态**：推送后等几秒让 workflow 注册，再用 `gh` CLI 查询：

   ```shell
   git push
   gh run list --branch $(git branch --show-current) --limit 3
   ```

2. **等待运行结束**：对最新一次 run 阻塞等待，失败时命令返回非零退出码：

   ```shell
   gh run watch <run-id> --exit-status --interval 15
   ```

3. **CI 报错必须修复**：查看失败日志，修复问题后按本文档规范重新 commit 并推送，再重复第 1～2 步，直到 CI 通过：

   ```shell
   gh run view <run-id> --log-failed
   ```

   - 修复产生的 commit 同样遵循上文格式（通常为 `fix:`）。
   - 失败明显与本次改动无关（平台故障、外部依赖抖动等）时，把日志和结论告诉用户，不要反复盲目重试。
   - 未安装 `gh` 或无法访问 GitHub 时，提醒用户自行到仓库的 Actions 页面确认结果。

## 项目特定规范

### 本项目的 Commit 风格

- 使用中文描述
- 简洁的 subject（通常一行）
- 多个改动时使用 body 列举
- 不强制添加 body，简单改动可以只有 subject

### 示例

```shell
# 简单改动
git commit -m "fix: 修复按钮点击无响应"

# 复杂改动
git commit -m "refactor: 重构暂停菜单为游戏内弹窗" -m @"
- 新增 PauseManager 管理暂停弹窗
- 修改 GameScene 集成暂停功能
- 从调试面板移除暂停场景
- 删除不必要的 resume 参数传递逻辑
"@
```

## 注意事项

- ✅ **一次性提交全部本地改动**：把本次所有改动作为一个 commit 提交，不按逻辑、文件或批次拆分。
- ❌ 不要使用 `git commit -am`（容易误添加不该提交的文件）
- ❌ PowerShell 7 中不要使用 bash 的 HEREDOC（`$(cat <<'EOF' ... EOF)`），会触发 `ParserError`；多行 message 改用多个 `-m` 或 here-string（见“创建 commit”）
- ❌ 扩展 here-string `@"..."@` 中的反引号和 `$` 会被转义/展开，导致 commit message body 乱码。需保留字面量（如行内代码 `\`xxx`\``）时改用字面 here-string `@'...'@`，或避免在 body 中使用反引号
- ❌ 不要使用不符合规范的 commit message
- ❌ 不要修改除了格式化以外的业务功能代码。如果提交被异常中断（比如编译失败），请你把问题列出来告诉用户，同时本次任务不提交代码。
- ✅ 每次提交前使用 `git status` 确认改动
- ✅ 大改动前先 `git pull` 确保代码是最新的
- ✅ 提交后使用 `git log -1` 检查 commit 信息
- ✅ 推送到 GitHub 且仓库配置了 Actions CI 时，推送后检查 Actions 状态；有报错必须修复后重新提交推送（见"推送后检查 GitHub Actions CI"）

现在请根据你的改动创建合适的 commit，然后执行git commit命令提交你的改动。
