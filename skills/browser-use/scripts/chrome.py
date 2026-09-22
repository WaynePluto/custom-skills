#!/usr/bin/env python3
"""通过已安装的 uv 工具环境运行 Chrome 专用入口，不下载依赖。"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from chrome_host import ChromeError

MAX_CODE = 64 * 1024


def runtime_python() -> Path:
    """查找 uv 管理的解释器，避免依赖调用者的 Python 包环境。"""
    roots = []
    if os.environ.get("UV_TOOL_DIR"):
        roots.append(Path(os.environ["UV_TOOL_DIR"]).expanduser())
    uv = shutil.which("uv")
    if uv:
        try:
            result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                roots.append(Path(result.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]) / "uv/tools")
    for root in roots:
        candidate = root / "browser-harness/Scripts/python.exe"
        if candidate.is_file():
            return candidate.resolve()
    raise ChromeError("runtime_missing", "未找到 uv 安装的 browser-harness 环境；请运行仓库的 sync --tools。")


def isolated_env(session: str) -> dict:
    """每个任务独立 IPC，清空可能改变连接目标的继承配置。"""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", session):
        raise ChromeError("invalid_session", "session 只允许 1–40 个英文字母、数字、下划线或连字符。")
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ChromeError("unsupported_platform", "本入口仅支持 Windows 本机 Chrome。")
    root = Path(local) / "custom-skills/browser-harness" / session
    env = dict(os.environ)
    # 使用空值阻止上游 .env 的 setdefault 重新注入远程或云端设置。
    for key in ("BU_CDP_WS", "BU_CDP_URL", "BU_BROWSER_ID", "BU_AUTOSPAWN", "BH_CHROME_PATH", "CHROME_PATH"):
        env[key] = ""
    env.update({
        "BU_NAME": "chrome",
        "BH_RUNTIME_DIR": str(root), "BH_TMP_DIR": str(root),
        "BH_RUNTIME_DIR_SHARED": "1", "BH_TMP_DIR_SHARED": "1",
        "BH_CONFIG_DIR": str(root / "config"), "BH_AGENT_WORKSPACE": str(root / "workspace"),
        "BH_REQUIRE_EXISTING_DAEMON": "1", "BH_DOMAIN_SKILLS": "0",
        "BH_TAB_MARKER": "0", "BH_RECORD": "0", "BH_TELEMETRY": "0",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": "",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    })
    return env


def parser():
    result = argparse.ArgumentParser(description="只连接个人 Chrome；不操作 Edge，不重启用户浏览器。")
    group = result.add_mutually_exclusive_group()
    group.add_argument("--doctor", action="store_true", help="只读诊断，不启动 Chrome 或 daemon")
    group.add_argument("--ensure", action="store_true", help="准备 Chrome 及连接；授权未完成时返回状态")
    group.add_argument("--stop", action="store_true", help="只停止本 session 的 daemon，不关闭 Chrome")
    result.add_argument("--session", default="personal", help="同一任务保持同一名称，不同任务使用不同名称")
    result.add_argument("--timeout", type=int, default=60, choices=range(1, 301), metavar="1..300",
                        help="脚本执行上限，默认 60 秒；不是 Chrome 授权等待时长")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if sys.platform != "win32":
            raise ChromeError("unsupported_platform", "本入口仅支持 Windows 本机 Chrome。")
        action = "doctor" if args.doctor else "ensure" if args.ensure else "stop" if args.stop else "run"
        code = ""
        if action == "run":
            if sys.stdin.isatty():
                raise ChromeError("script_required", "请通过 PowerShell here-string 管道传入 Python 脚本，或使用 --ensure。")
            code = sys.stdin.read(MAX_CODE + 1)
            if not code.strip() or len(code) > MAX_CODE:
                raise ChromeError("invalid_script", "脚本必须非空且不超过 65536 字符。")
        env = isolated_env(args.session)
        python = runtime_python()
        command = [str(python), "-I", "-X", "utf8", str(Path(__file__).with_name("chrome_runtime.py")), action]
        # 控制进程有界退出，授权中的官方辅助 daemon 单独保留至其 180 秒上限。
        result = subprocess.run(command, input=code, text=True, encoding="utf-8", env=env,
                                timeout=args.timeout + 25)
        return result.returncode
    except subprocess.TimeoutExpired:
        error = ChromeError("timeout", "执行超时，脚本未自动重试；Chrome 未关闭，已发出的页面操作不会回滚。")
    except (ChromeError, OSError) as exc:
        error = exc
    print(json.dumps({"ok": False, "status": getattr(error, "code", "runtime_error"), "message": str(error)}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
