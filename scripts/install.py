#!/usr/bin/env python3
"""构建并安装技能、context 文件和扩展到全局目录。"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 工具函数 ──

def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, shell=(sys.platform == "win32"))


def check_python_prerequisite() -> None:
    """检查所有安装路径共同需要的宿主 Python。"""
    if sys.version_info < (3, 10):
        sys.exit(f"错误：Python 版本过低：{platform.python_version()}（要求 >= 3.10）。")


def check_prerequisites() -> None:
    """检查 Node 相关部署路径需要的前置条件。"""
    if not shutil.which("node"):
        sys.exit("错误：未找到 Node.js（要求 >= 20）。请先安装 Node.js。")

    result = subprocess.run(
        ["node", "--version"], capture_output=True, text=True,
        shell=(sys.platform == "win32"),
    )
    version = result.stdout.strip().lstrip("v")
    major = int(version.split(".")[0])
    if major < 20:
        sys.exit(f"错误：Node.js 版本过低：v{version}（要求 >= 20）。")

    if not shutil.which("pnpm"):
        sys.exit("错误：未找到 pnpm。请先安装 pnpm（npm install -g pnpm）。")


def dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


# ── Context ──

def deploy_context(pi_agent_dir: Path, force: bool) -> None:
    """将 context/ 目录下的文件部署到 Pi agent 目录。"""
    context_dir = REPO_ROOT / "context"
    if not context_dir.exists():
        return

    context_files = list(context_dir.glob("*.md"))
    if not context_files:
        return

    print(f"\n{'═' * 4} context {'═' * 4}")

    pi_agent_dir.mkdir(parents=True, exist_ok=True)

    for src in context_files:
        dest = pi_agent_dir / src.name
        if dest.exists() and not force:
            print(f"  跳过 {src.name}（已存在，使用 --force 覆盖）")
            continue
        shutil.copy2(src, dest)
        print(f"  {src.name} → {dest}")


# ── Shell ──

def detect_pwsh() -> str | None:
    """检测 PowerShell 7（pwsh）可执行文件路径，找不到返回 None。"""
    found = shutil.which("pwsh") or shutil.which("pwsh.exe")

    if not found:
        # 常见安装位置兜底（仅用系统标准变量，不读取自定义 shell 偏好）
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PowerShell" / "7" / "pwsh.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "PowerShell" / "7" / "pwsh.exe",
        ]
        for c in candidates:
            if c.exists():
                found = str(c)
                break

    if not found:
        return None

    # 归一化扩展名为小写 .exe（shutil.which 在 Windows 上可能返回大写 .EXE）
    root, ext = os.path.splitext(found)
    return root + ext.lower() if ext else found


def configure_shell(pi_agent_dir: Path, force: bool) -> None:
    """在 Windows 上将 settings.json 的 shellPath 指向 PowerShell 7。

    context/APPEND_SYSTEM.md 假定工具 shell 是 pwsh；此处确保安装后实际 shell 与之一致。
    采用合并写法：只设置 shellPath 键，保留用户已有的其它配置。
    """
    if sys.platform != "win32":
        return

    print(f"\n{'═' * 4} shell {'═' * 4}")

    pwsh = detect_pwsh()
    if not pwsh:
        print("  跳过：未检测到 PowerShell 7（pwsh）。")
        print("    提示：APPEND_SYSTEM.md 假定 pwsh 作为工具 shell；")
        print("    请安装 PowerShell 7 后重跑，或手动在 settings.json 设置 shellPath。")
        return

    settings_path = pi_agent_dir / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"错误：{settings_path} 解析失败：{e}")
        if not isinstance(data, dict):
            sys.exit(f"错误：{settings_path} 顶层不是 JSON 对象，已跳过以避免破坏配置。")
    else:
        data = {}

    existing = data.get("shellPath")
    if existing and not force:
        print(f"  跳过 shellPath（已存在：{existing}，使用 --force 覆盖）")
        return

    data["shellPath"] = pwsh
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    action = "覆盖" if existing else "设置"
    print(f"  {action} shellPath → {pwsh}")


# ── Binaries ──

# 镜像 SDK utils/tools-manager.ts 的配置，保持仓库与 pi 下载到同一位置、同一命名。
GITHUB_API_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 120
USER_AGENT = "pi-coding-agent"


def _fd_asset(version: str, plat: str, machine: str) -> str | None:
    if plat == "darwin":
        return f"fd-v{version}-{machine}-apple-darwin.tar.gz"
    if plat == "linux":
        return f"fd-v{version}-{machine}-unknown-linux-gnu.tar.gz"
    if plat == "win32":
        return f"fd-v{version}-{machine}-pc-windows-msvc.zip"
    return None


def _rg_asset(version: str, plat: str, machine: str) -> str | None:
    if plat == "darwin":
        return f"ripgrep-{version}-{machine}-apple-darwin.tar.gz"
    if plat == "linux":
        if machine == "aarch64":
            return f"ripgrep-{version}-aarch64-unknown-linux-gnu.tar.gz"
        return f"ripgrep-{version}-x86_64-unknown-linux-musl.tar.gz"
    if plat == "win32":
        return f"ripgrep-{version}-{machine}-pc-windows-msvc.zip"
    return None


BINARY_TOOLS: dict[str, dict] = {
    "rg": {
        "display": "ripgrep",
        "repo": "BurntSushi/ripgrep",
        "binary": "rg",
        "tag_prefix": "",
        "system_names": ["rg"],
        "termux_package": "ripgrep",
        "asset": _rg_asset,
        "used_by": "grep 工具",
    },
    "fd": {
        "display": "fd",
        "repo": "sharkdp/fd",
        "binary": "fd",
        "tag_prefix": "v",
        "system_names": ["fd", "fdfind"],
        "termux_package": "fd",
        "asset": _fd_asset,
        "used_by": "find 工具",
    },
}


class BinaryDownloadError(RuntimeError):
    """二进制下载/安装失败；url 在已确定下载地址后才有值。"""

    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


def offline_mode() -> bool:
    """与 SDK 一致的 PI_OFFLINE 识别（空值为否，1/true/yes 为是）。"""
    value = os.environ.get("PI_OFFLINE")
    if not value:
        return False
    return value == "1" or value.lower() in ("true", "yes")


def is_termux() -> bool:
    """Termux 上 Linux 通用二进制因 Bionic libc 不兼容，只能走 pkg install。"""
    return "com.termux" in os.environ.get("PREFIX", "") or "ANDROID_ROOT" in os.environ


def host_platform() -> str | None:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def host_machine() -> str | None:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return None


def latest_release_version(repo: str) -> str:
    """获取 GitHub 最新 release 版本号。

    优先走 github.com/<repo>/releases/latest 的 302 重定向（不消耗 API 配额），
    失败后才回退到 api.github.com（未认证只有 60 次/小时，共享出口 IP 的环境很容易撞限）。
    """
    try:
        request = urllib.request.Request(
            f"https://github.com/{repo}/releases/latest", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=GITHUB_API_TIMEOUT) as response:
            tag = response.url.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"v?\d+(?:\.\d+)*", tag):
            return tag.lstrip("v")
    except Exception:
        pass  # 回退到 API

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=GITHUB_API_TIMEOUT) as response:
        data = json.load(response)
    tag = data.get("tag_name", "")
    if not tag:
        raise RuntimeError(f"{repo} 的 latest release 缺少 tag_name")
    return tag.lstrip("v")


def download_file(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def extract_archive(archive: Path, dest_dir: Path) -> None:
    """解压 .zip / .tar.gz，全部用标准库，不依赖外部 tar/unzip。"""
    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    elif name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            try:
                tf.extractall(dest_dir, filter="data")  # Python >= 3.12
            except TypeError:
                tf.extractall(dest_dir)
    else:
        raise RuntimeError(f"不支持的压缩格式：{name}")


def find_binary(root: Path, filename: str) -> Path | None:
    for path in root.rglob(filename):
        if path.is_file():
            return path
    return None


def download_binary(tool: str, bin_dir: Path) -> Path:
    """从 GitHub Releases 下载并安装到 bin_dir，返回二进制路径。

    失败时抛 BinaryDownloadError，带上已知的下载链接，供调用方拼手动下载指引。
    """
    config = BINARY_TOOLS[tool]
    plat = host_platform()
    machine = host_machine()
    if not plat or not machine:
        raise BinaryDownloadError(f"不支持的平台：{sys.platform}/{platform.machine()}")

    try:
        version = latest_release_version(config["repo"])
    except Exception as e:
        raise BinaryDownloadError(f"无法获取最新版本号：{e}") from e

    # SDK 里的同款修正：fd 的 macOS x64 构建在新版本里缺失
    if tool == "fd" and plat == "darwin" and machine == "x86_64":
        version = "10.3.0"

    asset = config["asset"](version, plat, machine)
    if not asset:
        raise BinaryDownloadError(f"不支持的平台：{plat}/{machine}")

    url = f"https://github.com/{config['repo']}/releases/download/{config['tag_prefix']}{version}/{asset}"
    suffix = ".exe" if plat == "win32" else ""
    binary_path = bin_dir / (config["binary"] + suffix)

    bin_dir.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {config['display']} {version} ...")

    try:
        with tempfile.TemporaryDirectory(dir=bin_dir, prefix=f"tmp_{config['binary']}_") as tmp:
            tmp_dir = Path(tmp)
            archive = tmp_dir / asset
            download_file(url, archive)
            extract_dir = tmp_dir / "extract"
            extract_dir.mkdir()
            extract_archive(archive, extract_dir)

            extracted = find_binary(extract_dir, config["binary"] + suffix)
            if not extracted:
                raise RuntimeError(f"压缩包里未找到 {config['binary'] + suffix}：{asset}")

            # 先落到同目录的临时名再原子替换，避免与正在运行的 pi 争用同一文件
            staged = bin_dir / f"{config['binary']}{suffix}.new"
            shutil.move(str(extracted), staged)
            if plat != "win32":
                staged.chmod(0o755)
            os.replace(staged, binary_path)
    except BinaryDownloadError:
        raise
    except Exception as e:
        raise BinaryDownloadError(str(e), url=url) from e

    return binary_path


def binary_version(path: str) -> str | None:
    """运行 `<binary> --version` 并提取版本号（如 "ripgrep 14.1.1 (rev ...)" → "14.1.1"）。"""
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"\d+(?:\.\d+)+", result.stdout)
    return match.group(0) if match else None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def is_outdated(current: str, latest: str) -> bool:
    try:
        return version_tuple(current) < version_tuple(latest)
    except ValueError:
        # 版本号格式意外时不猜，不相等即视为有更新
        return current != latest


def prompt_yes_no(question: str) -> bool:
    """交互式确认，默认否；非交互环境（管道/CI）直接返回否。"""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def check_and_upgrade(tool: str, local_path: Path) -> None:
    """已安装时检查 GitHub 上是否有新版，并询问是否升级。

    升级一律需要交互确认（输入 y），--force 不会自动升级；
    升级失败不中断安装，因为现有版本仍可用。
    """
    config = BINARY_TOOLS[tool]
    current = binary_version(str(local_path))
    current_text = current or "未知版本"

    if offline_mode():
        print(f"  {config['display']} {current_text}（PI_OFFLINE 已启用，跳过更新检查）")
        return

    try:
        latest = latest_release_version(config["repo"])
    except Exception as e:
        print(f"  {config['display']} {current_text}（更新检查失败：{e}）")
        return

    if current and not is_outdated(current, latest):
        print(f"  {config['display']} {current} 已是最新")
        return

    print(f"  {config['display']} 有可用更新：{current_text} → {latest}")
    if not prompt_yes_no(f"    更新 {config['display']} 到 {latest}？"):
        print("    保持现有版本。")
        return

    try:
        path = download_binary(tool, local_path.parent)
    except Exception as e:
        print(f"    失败：{config['display']} 更新未完成（{e}），已保留原有版本。")
        return
    print(f"    已更新 → {path}（{binary_version(str(path)) or latest}）")


def system_binary(tool: str, bin_dir: Path) -> str | None:
    """在 PATH 上查找系统自带的同名二进制。

    排除落在 bin_dir 里的命中：pi 会把自己的 bin 目录注入子 shell 的 PATH，
    不排除的话从 pi 里跑本脚本会把自管的二进制误判为系统二进制，从而跳过升级。
    """
    bin_dir_key = os.path.normcase(str(bin_dir.resolve())) if bin_dir.exists() else None
    for name in BINARY_TOOLS[tool]["system_names"]:
        found = shutil.which(name)
        if not found:
            continue
        if bin_dir_key and os.path.normcase(str(Path(found).resolve().parent)) == bin_dir_key:
            continue
        return found
    return None


def abort_missing_binary(tool: str, bin_dir: Path, error: Exception) -> None:
    """缺失的二进制下载失败：打印手动安装指引并中断安装。"""
    config = BINARY_TOOLS[tool]
    suffix = ".exe" if sys.platform == "win32" else ""
    url = getattr(error, "url", None) or f"https://github.com/{config['repo']}/releases/latest"
    sys.exit(
        f"\n错误：{config['display']} 下载失败（{error}），安装已中断。\n"
        f"  {config['used_by']}依赖 {config['binary']}，请手动处理：\n"
        f"  1. 下载：{url}\n"
        f"  2. 解压后把 {config['binary']}{suffix} 放到：{bin_dir / (config['binary'] + suffix)}\n"
        f"  3. 重新执行安装：python scripts/install.py"
    )


def deploy_binaries(pi_agent_dir: Path) -> None:
    """确保 grep / find 工具依赖的 rg / fd 已就绪，并在有新版时询问是否升级。

    pi CLI 会在启动时自行下载，但只用 GUI（pi-agent-chat）的机器上没有这个时机，
    只能等到模型首次调用 grep/find 时静默下载。安装阶段就备好，避免首次调用卡顿或失败。
    下载目标与 SDK 一致：<pi-agent-dir>/bin/，CLI 与 GUI 共用同一份。

    行为约定：
    - 缺失且下载失败 → 中断安装，提示手动下载并放入 bin 目录后重试。
    - 已存在且有新版 → 询问，输入 y 才升级；--force 不会绕过询问。
    - 系统 PATH 上自带的 rg/fd 只报告，不接管升级。
    """
    print(f"\n{'═' * 4} binaries {'═' * 4}")

    bin_dir = pi_agent_dir / "bin"
    suffix = ".exe" if sys.platform == "win32" else ""

    for tool, config in BINARY_TOOLS.items():
        local_path = bin_dir / (config["binary"] + suffix)
        if local_path.exists():
            check_and_upgrade(tool, local_path)
            continue

        system_path = system_binary(tool, bin_dir)
        if system_path:
            version = binary_version(system_path)
            print(
                f"  {config['display']} 使用系统 PATH 上的 {system_path}"
                + (f"（{version}）" if version else "")
                + "，不由本脚本管理升级。"
            )
            continue

        if offline_mode():
            print(f"  跳过 {config['display']}（PI_OFFLINE 已启用，不下载）；{config['used_by']}将不可用。")
            continue

        if is_termux():
            print(f"  跳过 {config['display']}（Termux 不兼容通用二进制），请执行：pkg install {config['termux_package']}")
            continue

        try:
            path = download_binary(tool, bin_dir)
        except Exception as e:
            abort_missing_binary(tool, bin_dir, e)

        version = binary_version(str(path))
        print(f"  {config['display']} → {path}" + (f"（{version}）" if version else ""))


# ── Extensions ──

def deploy_extensions(pi_agent_dir: Path, force: bool) -> None:
    """将 extensions/ 目录下的扩展部署到 Pi 扩展目录。"""
    extensions_dir = REPO_ROOT / "extensions"
    if not extensions_dir.exists():
        return

    ext_files = [p for p in extensions_dir.iterdir() if p.is_file() or p.is_dir()]
    if not ext_files:
        return

    print(f"\n{'═' * 4} extensions {'═' * 4}")

    dest_dir = pi_agent_dir / "extensions"
    dest_dir.mkdir(parents=True, exist_ok=True)

    for src in ext_files:
        dest = dest_dir / src.name
        if dest.exists() and not force:
            print(f"  跳过 {src.name}（已存在，使用 --force 覆盖）")
            continue
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        print(f"  {src.name} → {dest}")


# ── browser-use 技能 ──

# browser-use 技能依赖 browser-harness（github.com/browser-use/browser-harness），包名和 CLI 名不变。
# 上游说明保存到 references；本机入口由模板生成，防止升级覆盖 Chrome 连接策略。
# 仓库副本进 Git，全局说明、适配脚本由本段一起刷新。
BROWSER_HARNESS = "browser-harness"
BROWSER_USE_SKILL = "browser-use"
BROWSER_HARNESS_PYTHON = "3.12"


def parse_uv_tool_list(output: str) -> dict[str, str]:
    """解析 `uv tool list` 输出为 {包名: 版本}。

    每个已安装工具形如 "browser-harness v0.5.3"，其后的缩进行是可执行文件名。
    """
    tools: dict[str, str] = {}
    for line in output.splitlines():
        if not line or line[0].isspace():
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("v"):
            tools[parts[0]] = parts[1].lstrip("v")
    return tools


def apply_skill_overrides(text: str, overrides: dict[str, str]) -> str:
    """把 overrides 应用到 SKILL.md 的 frontmatter。

    frontmatter 内同名 key 整行替换，缺失的 key 插入到 frontmatter 末尾，正文不动。
    用于把官方生成的 name/description 换成本仓库审定的版本（AGENTS.md 要求
    description 必须说明何时使用与何时不使用，官方默认的 "Always use ..." 不满足）。
    """
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        front = ["---", *[f"{k}: {quoted(v)}" for k, v in overrides.items()], "---", ""]
        return "\n".join(front) + text

    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return text

    remaining = dict(overrides)
    for i in range(1, closing):
        matched = re.match(r"^([A-Za-z][\w-]*)\s*:", lines[i])
        if matched and matched.group(1) in remaining:
            key = matched.group(1)
            lines[i] = f"{key}: {quoted(remaining.pop(key))}"

    for offset, (key, value) in enumerate(remaining.items()):
        lines.insert(closing + offset, f"{key}: {quoted(value)}")
    return "\n".join(lines)


def uv_executable() -> str | None:
    return shutil.which("uv") or shutil.which("uv.exe")


def browser_harness_bin(uv: str) -> Path | None:
    """定位 uv 安装的 browser-harness 可执行文件。

    uv 安装后不保证其 bin 目录已在当前进程的 PATH 上，PATH 找不到时
    回落到 `uv tool dir --bin`，再回落到 uv 的默认位置 ~/.local/bin。
    """
    suffix = ".exe" if sys.platform == "win32" else ""
    found = shutil.which(BROWSER_HARNESS)
    if found:
        return Path(found)

    candidates: list[Path] = []
    try:
        result = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            candidates.append(Path(result.stdout.strip()))
    except Exception:
        pass  # 回落到默认位置
    candidates.append(Path.home() / ".local" / "bin")

    for directory in candidates:
        candidate = directory / (BROWSER_HARNESS + suffix)
        if candidate.exists():
            return candidate
    return None


def warn_if_browser_harness_not_on_path(harness: Path) -> None:
    """CLI 不在当前 PATH 时给出可直接执行的后续提示。"""
    if shutil.which(BROWSER_HARNESS):
        return

    print(f"  提示：{harness.parent} 不在当前 PATH 中，Agent 无法直接调用 {BROWSER_HARNESS}。")
    if sys.platform == "win32":
        print("        请将该目录加入用户 Path，然后彻底重启 VS Code。")
    else:
        print("        请将该目录加入 PATH，然后重新启动 Agent。")


def generate_browser_use_skill(source: Path, upstream: str) -> Path:
    """分别保存官方说明和本机入口，避免每次升级丢失连接约束。"""
    overrides = json.loads((source / "skill-overrides.json").read_text(encoding="utf-8"))
    if (not isinstance(overrides, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in overrides.items())
            or not all(overrides.get(key, "").strip() for key in ("name", "description"))):
        raise ValueError("skill-overrides.json 必须包含非空的 name/description，所有键值必须是字符串")
    template = (source / "skill-template.md").read_text(encoding="utf-8")
    body = apply_skill_overrides(template, overrides)
    reference = source / "references" / "upstream-skill.md"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(upstream, encoding="utf-8", newline="\n")
    skill = source / "SKILL.md"
    skill.write_text(body, encoding="utf-8", newline="\n")
    return skill


def warn_browser_harness_compatibility(source: Path, harness: Path) -> None:
    """升级保持原语义，但未经验证的版本不得静默接入个人浏览器。"""
    try:
        supported = json.loads((source / "scripts" / "compatibility.json").read_text(encoding="utf-8"))["browser_harness_versions"]
        result = subprocess.run([str(harness), "--version"], capture_output=True, text=True, timeout=10)
        current = result.stdout.strip() if result.returncode == 0 else "unknown"
        if current not in supported:
            print(f"  警告：browser-harness {current} 不在适配验证列表 {supported} 中。")
            print("        Chrome 专用入口会拒绝连接；请验证并更新适配器，不要回退到裸 CLI。")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"  警告：无法检查 Chrome 适配兼容性（{exc}）；使用前请运行 chrome.py --doctor。")


def deploy_browser_use(skills_dir: Path) -> None:
    """安装/升级 browser-harness 包，将 browser-use 技能同步到仓库与指定技能目录。

    与 rg/fd 不同，升级不经交互确认直接执行：sync 即升级是用户明确要求的语义。
    任何失败只跳过本段，不中断其它内容的部署。
    """
    print(f"\n{'═' * 4} {BROWSER_USE_SKILL}（依赖 {BROWSER_HARNESS}）{'═' * 4}")

    uv = uv_executable()
    if not uv:
        print("  跳过：未找到 uv。请先安装（winget install astral-sh.uv）后重跑。")
        return

    try:
        listing = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, timeout=30)
        installed = parse_uv_tool_list(listing.stdout)
    except Exception as e:
        print(f"  跳过：uv tool list 失败（{e}）。")
        return

    if BROWSER_HARNESS not in installed:
        if offline_mode():
            print("  跳过：PI_OFFLINE 已启用，且 browser-harness 尚未安装。")
            return
        print(f"  首次安装 {BROWSER_HARNESS}（uv 自管 Python {BROWSER_HARNESS_PYTHON}）...")
        result = run([uv, "tool", "install", "--python", BROWSER_HARNESS_PYTHON, BROWSER_HARNESS])
        if result.returncode != 0:
            print(f"  跳过：安装失败（退出码 {result.returncode}），不影响其它内容。")
            return
    elif offline_mode():
        print(f"  {BROWSER_HARNESS} {installed[BROWSER_HARNESS]}（PI_OFFLINE 已启用，跳过升级检查）")
    else:
        print(f"  升级 {BROWSER_HARNESS}（当前 {installed[BROWSER_HARNESS]}）...")
        result = run([uv, "tool", "upgrade", BROWSER_HARNESS])
        if result.returncode != 0:
            print(f"  提示：升级失败（退出码 {result.returncode}），用现有版本继续生成 SKILL.md。")

    harness = browser_harness_bin(uv)
    if not harness:
        print(f"  跳过：未定位到 {BROWSER_HARNESS} 可执行文件，无法生成 SKILL.md。")
        return
    warn_if_browser_harness_not_on_path(harness)

    try:
        result = subprocess.run([str(harness), "skill"], capture_output=True, text=True, encoding="utf-8", timeout=120)
    except Exception as e:
        print(f"  跳过：`{BROWSER_HARNESS} skill` 执行失败（{e}）。")
        return
    body = result.stdout
    if result.returncode != 0 or not body.strip():
        print(f"  跳过：`{BROWSER_HARNESS} skill` 无有效输出（退出码 {result.returncode}）。")
        return

    source = REPO_ROOT / "skills" / BROWSER_USE_SKILL
    try:
        repo_skill = generate_browser_use_skill(source, body)
    except (OSError, ValueError) as exc:
        print(f"  跳过：Chrome 专用技能生成失败（{exc}），不部署上游裸 CLI 工作流。")
        return
    warn_browser_harness_compatibility(source, harness)
    # --tools 也要刷新运行入口，不能只复制 SKILL.md 留下旧脚本。
    deploy_generic(source, skills_dir / BROWSER_USE_SKILL, force=True)
    print(f"  已生成 Chrome 专用 SKILL.md；上游说明另存 references；仓库：{repo_skill}")
    print(f"  已同步说明和适配脚本 → {skills_dir / BROWSER_USE_SKILL}")


# ── computer-use 技能 ──

COMPUTER_USE_SKILL = "computer-use"
COMPUTER_USE_PYTHON = "3.13"
COMPUTER_USE_RUNTIME_ENV = "COMPUTER_USE_RUNTIME_DIR"


def computer_use_runtime_dir() -> Path:
    """返回 computer-use 专用运行时目录；测试可通过环境变量隔离。"""
    configured = os.environ.get(COMPUTER_USE_RUNTIME_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{COMPUTER_USE_RUNTIME_ENV} 必须是绝对路径")
        return path
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ValueError("缺少 LOCALAPPDATA，无法定位 computer-use 运行时")
    return Path(local) / "custom-skills" / COMPUTER_USE_SKILL / "runtime"


def computer_use_runtime_python(runtime_dir: Path) -> Path:
    """返回 Windows venv 的解释器路径。"""
    return runtime_dir / "Scripts" / "python.exe"


def verify_computer_use_runtime(source: Path, runtime_dir: Path) -> tuple[bool, str]:
    """不创建原生 runtime，仅验证发行版、绑定/DLL 哈希和 SDK 接口。"""
    python = computer_use_runtime_python(runtime_dir)
    if not python.is_file():
        return False, "缺少 Python 解释器"
    verifier = source / "scripts" / "backend.py"
    try:
        checked = subprocess.run(
            [str(python), "-I", "-X", "utf8", str(verifier)],
            capture_output=True, text=True, encoding="utf-8", timeout=90,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "PYTHONPATH": "", "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    try:
        payload = json.loads(checked.stdout)
    except (TypeError, json.JSONDecodeError):
        return False, f"验证器退出码 {checked.returncode}，未返回有效 JSON"
    if not isinstance(payload, dict):
        return False, "验证器未返回 JSON 对象"
    if checked.returncode != 0 or payload.get("ok") is not True:
        return False, str(payload.get("status", "incompatible_version"))[:80]
    if payload.get("native_runtime_created") is not False or payload.get("telemetry_enabled") is not False:
        return False, "验证过程未明确证明禁用遥测且未创建原生 runtime"
    if not isinstance(payload.get("cua_driver"), str) or not payload["cua_driver"]:
        return False, "验证器缺少 cua-driver 版本"
    return True, payload["cua_driver"]


@contextmanager
def computer_use_install_lock(runtime: Path):
    """使用自动释放的文件锁串行化同一运行时的安装。"""
    runtime.parent.mkdir(parents=True, exist_ok=True)
    lock_path = runtime.parent / ".install.lock"
    stream = lock_path.open("a+b")
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        import msvcrt
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("另一个 computer-use 安装正在进行") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        stream.close()


def recover_computer_use_runtime(destination: Path) -> None:
    """恢复被中断的切换；同时存在新旧目录时保守停止。"""
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists() and not destination.exists():
        os.replace(backup, destination)
    elif backup.exists():
        raise RuntimeError(f"发现待人工核对的运行时备份：{backup}")


def _replace_runtime(staged: Path, destination: Path) -> Path | None:
    """切换已验证运行时；保留旧目录，待移动后校验通过再删除。"""
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists():
        raise RuntimeError(f"发现未处理的运行时备份：{backup}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    had_previous = destination.exists()
    if had_previous:
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        if had_previous and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    return backup if had_previous else None


def _restore_runtime(destination: Path, backup: Path | None) -> None:
    """删除失败的新环境，并在存在备份时恢复。"""
    if destination.exists():
        shutil.rmtree(destination)
    if backup is not None and backup.exists():
        os.replace(backup, destination)


def deploy_computer_use_skill(
    source: Path, destination: Path, *, keep_backup: bool = False
) -> Path | None:
    """用同文件系统 staging 原子刷新技能目录，可为跨目录事务保留旧版本。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".computer-use-skill-", dir=str(destination.parent)))
    staged = staging_root / COMPUTER_USE_SKILL
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
        raise RuntimeError(f"发现待人工核对的技能备份：{backup}")
    try:
        deploy_generic(source, staged, force=True)
        for relative in ("SKILL.md", "scripts/computer.py", "scripts/compatibility.json"):
            if not (staged / relative).is_file():
                raise RuntimeError(f"staging 缺少必要文件：{relative}")
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        saved_backup = backup if backup.exists() else None
        if saved_backup is not None and not keep_backup:
            try:
                shutil.rmtree(saved_backup)
                saved_backup = None
            except OSError as exc:
                # 新技能已完整切换；保留旧备份比回退 runtime 造成版本错配更安全。
                print(f"  警告：旧技能备份清理失败（{exc}）：{saved_backup}")
        return saved_backup
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _computer_use_transaction_path(runtime: Path) -> Path:
    return runtime.parent / ".install-transaction.json"


def _write_computer_use_transaction(runtime: Path, value: dict) -> None:
    """原子写安装事务阶段，供进程中断后的下一次安装恢复。"""
    path = _computer_use_transaction_path(runtime)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _restore_install_target(destination: Path, backup: Path, had_previous: bool) -> None:
    """按事务开始时的存在性恢复单个目录。"""
    switched = backup.exists() or (not had_previous and destination.exists())
    if not switched:
        return
    if destination.exists():
        shutil.rmtree(destination)
    if had_previous:
        if not backup.exists():
            raise RuntimeError(f"安装事务缺少可恢复备份：{backup}")
        os.replace(backup, destination)


def recover_computer_use_transaction(runtime: Path, skill_destination: Path) -> None:
    """恢复或收尾被中断的 runtime + skill 双目录事务。"""
    marker = _computer_use_transaction_path(runtime)
    if not marker.exists():
        return
    try:
        transaction = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"computer-use 安装事务记录损坏：{marker}") from exc
    if (transaction.get("runtime") != str(runtime)
            or transaction.get("skill") != str(skill_destination)):
        raise RuntimeError(f"computer-use 安装事务目标不匹配：{marker}")
    runtime_backup = runtime.with_name(runtime.name + ".previous")
    skill_backup = skill_destination.with_name(skill_destination.name + ".previous")
    if transaction.get("phase") == "committed":
        for backup in (runtime_backup, skill_backup):
            if backup.exists():
                shutil.rmtree(backup)
        marker.unlink(missing_ok=True)
        return
    _restore_install_target(
        skill_destination, skill_backup, bool(transaction.get("skill_had_previous"))
    )
    _restore_install_target(
        runtime, runtime_backup, bool(transaction.get("runtime_had_previous"))
    )
    marker.unlink(missing_ok=True)


def _deploy_computer_use_locked(skills_dir: Path, source: Path, runtime: Path) -> None:
    """在安装锁内准备运行时并部署技能。"""
    skill_destination = skills_dir / COMPUTER_USE_SKILL
    recover_computer_use_transaction(runtime, skill_destination)
    recover_computer_use_runtime(runtime)
    existing_ok, existing_version = verify_computer_use_runtime(source, runtime)
    if offline_mode():
        if not existing_ok:
            print(f"  跳过：PI_OFFLINE 已启用，且现有运行时不可用（{existing_version}）。")
            return
        print(f"  cua-driver {existing_version}（PI_OFFLINE 已启用，复用审定运行时）")
        deploy_computer_use_skill(source, skill_destination)
        return

    uv = uv_executable()
    if not uv:
        print("  跳过：未找到 uv。请先安装（winget install astral-sh.uv）后重跑。")
        return
    staged = Path(tempfile.mkdtemp(prefix=".runtime-next-", dir=str(runtime.parent)))
    try:
        created = run([uv, "venv", "--python", COMPUTER_USE_PYTHON, str(staged)])
        if created.returncode != 0:
            print(f"  跳过：创建 Python {COMPUTER_USE_PYTHON} 运行时失败（退出码 {created.returncode}）。")
            return
        staged_python = computer_use_runtime_python(staged)
        installed = run([
            uv, "pip", "install", "--python", str(staged_python), "--require-hashes",
            "--requirement", str(source / "scripts" / "requirements.lock"),
        ])
        if installed.returncode != 0:
            print(f"  跳过：安装 cua-driver 失败（退出码 {installed.returncode}），保留现有运行时。")
            return
        valid, detail = verify_computer_use_runtime(source, staged)
        if not valid:
            print(f"  跳过：新运行时兼容性校验失败（{detail}），保留现有运行时。")
            return
        transaction = {
            "phase": "prepared",
            "runtime": str(runtime),
            "skill": str(skill_destination),
            "runtime_had_previous": runtime.exists(),
            "skill_had_previous": skill_destination.exists(),
        }
        _write_computer_use_transaction(runtime, transaction)
        try:
            runtime_backup = _replace_runtime(staged, runtime)
            transaction["phase"] = "runtime_switched"
            _write_computer_use_transaction(runtime, transaction)
            moved_ok, moved_detail = verify_computer_use_runtime(source, runtime)
            if not moved_ok:
                raise RuntimeError(f"运行时切换后校验失败：{moved_detail}")
            skill_backup = deploy_computer_use_skill(
                source, skill_destination, keep_backup=True
            )
            transaction["phase"] = "skill_switched"
            _write_computer_use_transaction(runtime, transaction)
            transaction["phase"] = "committed"
            _write_computer_use_transaction(runtime, transaction)
            for backup in (runtime_backup, skill_backup):
                if backup is not None and backup.exists():
                    shutil.rmtree(backup)
            _computer_use_transaction_path(runtime).unlink(missing_ok=True)
        except BaseException:
            recover_computer_use_transaction(runtime, skill_destination)
            raise
        print(f"  cua-driver {detail} 运行时 → {runtime}")
        print(f"  已同步 computer-use 技能 → {skill_destination}")
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def deploy_computer_use(skills_dir: Path) -> None:
    """准备审定的 cua-driver 运行时并刷新 computer-use 技能。"""
    print(f"\n{'═' * 4} {COMPUTER_USE_SKILL}（依赖 cua-driver）{'═' * 4}")
    source = REPO_ROOT / "skills" / COMPUTER_USE_SKILL
    if sys.platform != "win32":
        print("  跳过：computer-use 首版仅支持 Windows x64。")
        return
    try:
        runtime = computer_use_runtime_dir()
        with computer_use_install_lock(runtime):
            _deploy_computer_use_locked(skills_dir, source, runtime)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"  跳过：computer-use 运行时准备失败（{exc}），不影响其它内容。")


def deploy_tools(skills_dir: Path, name: str | None) -> None:
    """按名称部署工具型技能；名称为空时部署全部。"""
    deployers = {
        BROWSER_USE_SKILL: deploy_browser_use,
        COMPUTER_USE_SKILL: deploy_computer_use,
    }
    if name is not None:
        deployers[name](skills_dir)
        return
    for deploy in deployers.values():
        deploy(skills_dir)


# ── Skills ──

def build_skill(skill_dir: Path) -> None:
    """在仓库根安装 workspace 依赖（幂等，只需一次）。"""
    pkg_path = skill_dir / "package.json"
    if not pkg_path.exists():
        return

    print("  安装依赖...")
    r = run(["pnpm", "install", "--ignore-scripts"], cwd=REPO_ROOT)
    if r.returncode != 0:
        sys.exit(f"pnpm install 失败（退出码 {r.returncode}）")


def deploy_with_script(skill_dir: Path, destination: Path, force: bool) -> None:
    """使用技能自带的 deploy.py 部署。"""
    deploy_script = skill_dir / "scripts" / "deploy.py"
    deploy_args = [sys.executable, str(deploy_script), "--destination", str(destination)]
    if force:
        deploy_args.append("--force")
    r = run(deploy_args)
    if r.returncode != 0:
        sys.exit(f"部署失败（退出码 {r.returncode}）")


def deploy_generic(skill_dir: Path, destination: Path, force: bool) -> None:
    """通用部署：复制必要文件，安装生产依赖。"""
    if destination.exists():
        if not force:
            print(f"  跳过（已存在：{destination}，使用 --force 覆盖）")
            return
        shutil.rmtree(destination)

    # 生成模板和 frontmatter 覆盖属于同步元数据，不部署给 Agent。
    exclude = {"tests", "node_modules", ".gitignore", "dist", "skill-overrides.json", "skill-template.md", "__pycache__"}
    destination.mkdir(parents=True, exist_ok=True)

    for item in skill_dir.iterdir():
        if item.name in exclude or item.suffix == ".pyc":
            continue
        dest_item = destination / item.name
        if item.is_dir():
            shutil.copytree(item, dest_item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, dest_item)

    if (destination / "package.json").exists():
        run(["pnpm", "install", "--prod", "--ignore-scripts", "--ignore-workspace"], cwd=destination)

    print(f"  部署完成（{dir_size_mb(destination):.1f} MB）")


def deploy_skills(
    sources_dir: Path,
    skills_dir: Path,
    name: str | None,
    force: bool,
    skip_names: set[str] | None = None,
) -> None:
    """从指定源码目录构建并部署技能。"""
    skip_names = skip_names or set()
    if name:
        skill_dirs = [sources_dir / name]
        if not skill_dirs[0].exists():
            sys.exit(f"技能不存在：{name}（路径 {skill_dirs[0]}）")
    else:
        skill_dirs = sorted(
            directory for directory in sources_dir.iterdir()
            if directory.is_dir() and directory.name not in skip_names
        )
        if not skill_dirs:
            print("未发现任何技能。")
            return

    for skill_dir in skill_dirs:
        skill_name = skill_dir.name
        if not (skill_dir / "SKILL.md").exists():
            print(f"跳过 {skill_name}（缺少 SKILL.md）")
            continue

        print(f"\n{'═' * 4} {skill_name} {'═' * 4}")

        destination = skills_dir / skill_name
        if destination.exists() and not force:
            # 提前跳过，避免为已存在的技能白跑一次 pnpm install
            print(f"  跳过（已存在：{destination}，使用 --force 覆盖）")
            continue

        build_skill(skill_dir)

        deploy_script = skill_dir / "scripts" / "deploy.py"

        print(f"  部署到 {destination} ...")
        if deploy_script.exists():
            deploy_with_script(skill_dir, destination, force)
        else:
            deploy_generic(skill_dir, destination, force)


# ── 入口 ──

def main() -> None:
    parser = argparse.ArgumentParser(
        description="构建并安装全局或项目级技能、context 文件和扩展"
    )
    parser.add_argument(
        "--name", default=None,
        help="要安装的技能名称（全局技能可省略；项目级技能必须指定）",
    )
    parser.add_argument(
        "--skills-dir", type=Path,
        default=Path.home() / ".agents" / "skills",
        help="全局 Skills 目录路径",
    )
    parser.add_argument(
        "--pi-agent-dir", type=Path,
        default=Path.home() / ".pi" / "agent",
        help="Pi agent 配置目录路径",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="覆盖已存在的文件（不影响二进制：rg/fd 升级始终需要交互确认）",
    )
    parser.add_argument("--skills", action="store_true", help="安装技能")
    parser.add_argument("--context", action="store_true", help="部署 context 文件（如 APPEND_SYSTEM.md）")
    parser.add_argument("--extensions", action="store_true", help="部署扩展")
    parser.add_argument(
        "--binaries", action="store_true",
        help="检查/下载 grep、find 依赖的 rg、fd 到 <pi-agent-dir>/bin/（随 --extensions 自动执行；升级需交互确认）",
    )
    parser.add_argument(
        "--tools", action="store_true",
        help="准备工具型技能运行时并同步技能；可用 --name 选择 browser-use 或 computer-use",
    )
    parser.add_argument(
        "--project-skills", action="store_true",
        help="将 project-skills/ 中的指定技能部署到目标项目",
    )
    parser.add_argument(
        "--project-dir", type=Path,
        help="目标项目根目录（与 --project-skills、--name 一起使用）",
    )
    args = parser.parse_args()

    selected_categories = any((args.skills, args.context, args.extensions,
                               args.binaries, args.tools, args.project_skills))
    if args.name and not selected_categories:
        # README 中的 `--name <skill>` 语义是只部署指定普通技能。
        args.skills = True
    if args.tools and args.name not in (None, BROWSER_USE_SKILL, COMPUTER_USE_SKILL):
        parser.error(f"--tools 不支持技能：{args.name}")

    if args.project_skills:
        if args.project_dir is None:
            parser.error("使用 --project-skills 时必须指定 --project-dir")
        if args.name is None:
            parser.error("使用 --project-skills 时必须指定 --name，不支持安装全部项目级技能")
        if args.skills or args.context or args.extensions or args.binaries or args.tools:
            parser.error("--project-skills 不能与 --skills、--context、--extensions、--binaries 或 --tools 组合使用")
        args.project_dir = args.project_dir.expanduser().resolve()
        if not args.project_dir.is_dir():
            parser.error(f"目标项目目录不存在或不是目录：{args.project_dir}")
    elif args.project_dir is not None:
        parser.error("--project-dir 只能与 --project-skills 一起使用")

    # 不加选择参数和名称时安装全部；--name 单独使用只复制对应普通技能。
    install_all = not (
        args.skills or args.context or args.extensions or args.binaries or args.tools
        or args.project_skills or args.name
    )

    check_python_prerequisite()
    needs_node = install_all or args.skills or args.extensions or args.binaries \
        or (args.tools and args.name in (None, BROWSER_USE_SKILL))
    if needs_node:
        check_prerequisites()

    if install_all or args.context:
        deploy_context(args.pi_agent_dir, args.force)
        # APPEND_SYSTEM.md 假定工具 shell 为 pwsh，需同步配置 settings.json
        configure_shell(args.pi_agent_dir, args.force)

    # enable-core-tools 扩展激活的 grep/find 依赖 rg/fd，先备齐依赖再装扩展；
    # 缺失且下载失败会在这里中断，--force 不影响二进制升级（升级一律需要确认）
    if install_all or args.extensions or args.binaries:
        deploy_binaries(args.pi_agent_dir)

    if install_all or args.extensions:
        deploy_extensions(args.pi_agent_dir, args.force)

    # 先准备工具型技能依赖并刷新其完整技能目录，在同一次 sync 中即可生效。
    if install_all or args.tools:
        deploy_tools(args.skills_dir, args.name if args.tools else None)

    if install_all or (args.skills and args.tools and args.name is None):
        deploy_skills(
            REPO_ROOT / "skills", args.skills_dir, None, args.force,
            skip_names={BROWSER_USE_SKILL, COMPUTER_USE_SKILL},
        )
    elif args.skills and not args.tools:
        deploy_skills(REPO_ROOT / "skills", args.skills_dir, args.name, args.force)

    if args.project_skills:
        deploy_skills(
            REPO_ROOT / "project-skills",
            args.project_dir / ".agents" / "skills",
            args.name,
            args.force,
        )

    print("\n全部完成。")


if __name__ == "__main__":
    main()
