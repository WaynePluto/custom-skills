#!/usr/bin/env python3
"""computer-use 标准库 CLI、专用 runtime 定位与一次性进程监督。"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from contracts import (
    ACTION_COMMANDS,
    MAX_INPUT_BYTES,
    MAX_JSON_BYTES,
    ToolError,
    dumps_result,
    elapsed_ms,
    error_result,
    validate_request,
    validate_text,
)

RUNTIME_ENV = "COMPUTER_USE_RUNTIME_PYTHON"
RUNTIME_DIR_ENV = "COMPUTER_USE_RUNTIME_DIR"


class ContractParser(argparse.ArgumentParser):
    """把 argparse 的进程退出转换为单 JSON 错误。"""

    def error(self, message: str) -> None:
        raise ToolError("invalid_argument", f"命令行参数无效：{message[:300]}")


class SupervisedJob:
    """Windows Job Object；请求仅在成功纳管 worker 后才写入 stdin。"""

    def __init__(self) -> None:
        self.handle: Any = None
        self.kernel32: Any = None
        self.name = rf"Local\CustomSkillsComputerUse-{os.getpid()}-{uuid.uuid4().hex}"

    def create(self) -> None:
        if sys.platform != "win32":
            raise ToolError("unsupported_platform", "computer-use 仅支持 Windows。")
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, self.name)
        if not handle:
            raise ToolError("execution_error", "无法创建 worker Job Object。")
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # 关闭父句柄时终止整个 Job Object
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(handle)
            raise ToolError("execution_error", "无法配置 worker Job Object。")
        self.handle = handle
        self.kernel32 = kernel32

    def assign(self, process: subprocess.Popen[str]) -> None:
        import ctypes
        from ctypes import wintypes
        self.kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = getattr(process, "_handle", None)
        if not process_handle or not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            self.close()
            try:
                process.kill()
            except OSError:
                pass
            raise ToolError("execution_error", "worker 未能在执行请求前加入受监督进程树。")

    def close(self) -> None:
        if self.handle is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "SupervisedJob":
        self.create()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def runtime_python() -> Path:
    """定位 uv 管理的 computer-use 专用 Python 3.13 环境，不安装依赖。"""
    candidates: list[Path] = []
    explicit = os.environ.get(RUNTIME_ENV)
    if explicit:
        candidates.append(Path(explicit).expanduser())
    runtime_dir = os.environ.get(RUNTIME_DIR_ENV)
    if runtime_dir:
        base = Path(runtime_dir).expanduser()
        candidates.extend([base / "Scripts" / "python.exe", base / ".venv" / "Scripts" / "python.exe"])
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "custom-skills" / "computer-use" / "runtime"
        candidates.extend([base / "Scripts" / "python.exe", base / ".venv" / "Scripts" / "python.exe"])
    tool_roots: list[Path] = []
    if os.environ.get("UV_TOOL_DIR"):
        tool_roots.append(Path(os.environ["UV_TOOL_DIR"]).expanduser())
    uv = shutil.which("uv") or shutil.which("uv.exe")
    if uv:
        try:
            found = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True,
                                   encoding="utf-8", timeout=5)
            if found.returncode == 0 and found.stdout.strip():
                tool_roots.append(Path(found.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    if os.environ.get("APPDATA"):
        tool_roots.append(Path(os.environ["APPDATA"]) / "uv" / "tools")
    for root in tool_roots:
        candidates.extend([
            root / "computer-use" / "Scripts" / "python.exe",
        ])
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve()
    raise ToolError("runtime_missing", "未找到 uv 管理的 computer-use 专用 runtime；请运行仓库 sync --tools。")


def _read_stdin_with_deadline(deadline: float) -> str:
    """限时限长读取 type 正文；不在错误中包含正文。"""
    if sys.stdin.isatty():
        raise ToolError("invalid_argument", "type 正文必须通过 stdin 管道传入。")
    output: queue.Queue[tuple[bytes | None, BaseException | None]] = queue.Queue(maxsize=1)

    def reader() -> None:
        chunks = bytearray()
        error: BaseException | None = None
        try:
            descriptor = sys.stdin.fileno()
            while len(chunks) <= MAX_INPUT_BYTES:
                chunk = os.read(descriptor, min(1024, MAX_INPUT_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
        except BaseException as exc:
            error = exc
        try:
            output.put_nowait((bytes(chunks), error))
        except queue.Full:
            pass

    thread = threading.Thread(target=reader, name="computer-use-stdin", daemon=True)
    thread.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError("timeout", "读取 type 正文前总预算已耗尽。")
    thread.join(remaining)
    if thread.is_alive():
        raise ToolError("timeout", "读取 type 正文超时；未下发桌面动作。")
    raw, read_error = output.get_nowait()
    if read_error is not None or raw is None:
        raise ToolError("invalid_argument", "无法读取 type 正文。")
    if len(raw) > MAX_INPUT_BYTES:
        raise ToolError("invalid_argument", "type 正文 UTF-8 字节数超限。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("invalid_argument", "type 正文必须是 UTF-8。") from exc
    return validate_text(text)


def parser() -> ContractParser:
    root = ContractParser(description="通过受监督的一次性进程观察和操作 Windows 桌面。")
    root.add_argument("--session")
    root.add_argument("--request-id")
    root.add_argument("--timeout", type=float, default=30.0)
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor")
    commands.add_parser("windows")

    focus = commands.add_parser("focus")
    focus.add_argument("--window", required=True)
    focus.add_argument("--delivery", choices=["background", "foreground"], default="background")
    focus.add_argument("--max-elements", type=int, default=200)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--window", required=True)
    snapshot.add_argument("--max-elements", type=int, default=200)

    screenshot = commands.add_parser("screenshot")
    shot_target = screenshot.add_mutually_exclusive_group(required=True)
    shot_target.add_argument("--window")
    shot_target.add_argument("--display", type=int)

    def add_target(command: argparse.ArgumentParser, *, element_only: bool = False) -> None:
        command.add_argument("--delivery", choices=["background", "foreground"], default="background")
        command.add_argument("--snapshot", required=True)
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--element")
        if not element_only:
            target.add_argument("--point", nargs=2, type=int)
        command.add_argument("--space", choices=["image"])
        command.add_argument("--window")

    click = commands.add_parser("click")
    add_target(click)
    click.add_argument("--button", choices=["left", "right", "middle"], default="left")
    click.add_argument("--clicks", type=int, choices=[1, 2], default=1)

    type_command = commands.add_parser("type")
    add_target(type_command, element_only=True)
    type_command.add_argument("--clear", action="store_true")
    type_command.add_argument("--press-enter", action="store_true")

    shortcut = commands.add_parser("shortcut")
    shortcut.add_argument("--delivery", choices=["background", "foreground"], default="background")
    shortcut.add_argument("--snapshot", required=True)
    shortcut.add_argument("--window", required=True)
    shortcut.add_argument("--keys", required=True)

    scroll = commands.add_parser("scroll")
    add_target(scroll)
    scroll.add_argument("--direction", choices=["up", "down", "left", "right"], default="down")
    scroll.add_argument("--amount", type=int, default=3)

    move = commands.add_parser("move")
    add_target(move)

    drag = commands.add_parser("drag")
    drag.add_argument("--delivery", choices=["background", "foreground"], default="background")
    drag.add_argument("--snapshot", required=True)
    source = drag.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-element")
    source.add_argument("--from-point", nargs=2, type=int)
    target = drag.add_mutually_exclusive_group(required=True)
    target.add_argument("--to-element")
    target.add_argument("--to-point", nargs=2, type=int)
    drag.add_argument("--space", choices=["image"])
    drag.add_argument("--window")
    drag.add_argument("--duration", type=float, default=0.5)

    wait = commands.add_parser("wait")
    wait.add_argument("--window", required=True)
    wait.add_argument("--condition", choices=["foreground", "appears", "disappears", "text"],
                      default="foreground")
    wait.add_argument("--name")
    wait.add_argument("--control-type")
    wait.add_argument("--text")
    wait.add_argument("--wait-seconds", type=float, default=10.0)
    wait.add_argument("--max-elements", type=int, default=200)

    commands.add_parser("cleanup")
    return root


def _request_from_args(args: argparse.Namespace, input_text: str | None) -> dict[str, Any]:
    values = vars(args).copy()
    command = values.pop("command")
    session = values.pop("session")
    request_id = values.pop("request_id")
    timeout = values.pop("timeout")
    request: dict[str, Any] = {
        "schema_version": 1, "command": command, "session": session,
        "request_id": request_id, "timeout": timeout,
        "args": {key: value for key, value in values.items() if value is not None},
    }
    if input_text is not None:
        request["input_text"] = input_text
    validate_request(request)
    return request


def _worker_env(job_name: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": "",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0", "CUA_TELEMETRY_ENABLED": "0",
        "COMPUTER_USE_SUPERVISED": "1", "COMPUTER_USE_JOB_NAME": job_name,
    })
    return env


def _run_worker(request: dict[str, Any], deadline: float) -> dict[str, Any]:
    python = runtime_python()
    runtime = Path(__file__).with_name("runtime.py")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError("timeout", "启动 worker 前总预算已耗尽。")
    with SupervisedJob() as job:
        command = [str(python), "-I", "-X", "utf8", str(runtime)]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=_worker_env(job.name),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ToolError("runtime_missing", "无法启动 computer-use 专用 runtime。",
                            details={"exception": type(exc).__name__}) from exc
        job.assign(process)
        try:
            stdout, _stderr = process.communicate(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                timeout=max(0.01, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as exc:
            job.close()  # KILL_ON_JOB_CLOSE 回收整个 worker 进程树。
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            side_effect = "uncertain" if request["command"] in ACTION_COMMANDS else "none"
            details = None
            if side_effect == "uncertain":
                details = {
                    "manual_check": "请先确认鼠标按钮及 Ctrl/Shift/Alt/Win 均已释放，再重新观察；不要重发动作。"
                }
            raise ToolError("timeout", "执行超时；未自动重试，动作是否已下发未知。",
                            side_effect=side_effect, details=details) from exc
        except Exception as exc:
            job.close()
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
            raise ToolError("execution_error", "worker 通信异常，未自动重试。",
                            side_effect="uncertain" if request["command"] in ACTION_COMMANDS else "none",
                            details={"exception": type(exc).__name__}) from exc
    if len(stdout.encode("utf-8")) > MAX_JSON_BYTES + 1024:
        raise ToolError("execution_error", "worker 输出超过协议上限。",
                        side_effect="uncertain" if request["command"] in ACTION_COMMANDS else "none")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("execution_error", "worker 未返回单个有效 JSON 对象。",
                        side_effect="uncertain" if request["command"] in ACTION_COMMANDS else "none",
                        details={"worker_exit": process.returncode}) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("ok"), bool):
        raise ToolError("execution_error", "worker 返回的协议对象无效。",
                        side_effect="uncertain" if request["command"] in ACTION_COMMANDS else "none")
    return payload


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    session = None
    request_id = None
    action_in_flight = False
    try:
        if sys.platform != "win32":
            raise ToolError("unsupported_platform", "computer-use 仅支持 Windows 11 x64。")
        args = parser().parse_args(argv)
        session = args.session
        request_id = args.request_id
        if not (1 <= args.timeout <= 120):
            raise ToolError("invalid_argument", "timeout 必须在 1–120 秒之间。")
        deadline = started + args.timeout
        input_text = _read_stdin_with_deadline(deadline) if args.command == "type" else None
        request = _request_from_args(args, input_text)
        request["deadline_monotonic"] = deadline
        validate_request(request)
        session = request.get("session")
        request_id = request.get("request_id")
        action_in_flight = request["command"] in ACTION_COMMANDS
        payload = _run_worker(request, deadline)
    except ToolError as error:
        payload = error_result(error, session=session, request_id=request_id,
                               elapsed_ms=elapsed_ms(started))
    except Exception as exc:
        payload = error_result(
            ToolError("execution_error", "CLI 未处理异常。",
                      side_effect="uncertain" if action_in_flight else "none",
                      details={"exception": type(exc).__name__}),
            session=session, request_id=request_id, elapsed_ms=elapsed_ms(started),
        )
    sys.stdout.write(dumps_result(payload) + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
