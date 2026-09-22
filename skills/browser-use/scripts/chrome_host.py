"""只探测 Windows 稳定版 Chrome，端点必须归属于指定可执行文件。"""

import ctypes
from ctypes import wintypes as w
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


class ChromeError(RuntimeError):
    """携带可供入口分类处理的稳定错误码。"""

    code: str

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _windows():
    if sys.platform != "win32":
        raise ChromeError("unsupported_platform", "Chrome 宿主探测仅支持 Windows。")


def _win_error(operation, number=None):
    number = ctypes.get_last_error() if number is None else number
    code = "permission_denied" if number == 5 else "process_query_failed"
    return ChromeError(code, f"{operation}失败（Windows error {number}）；不会据此启动 Chrome。")


@lru_cache(maxsize=None)
def _api(library, name, arguments, result=w.BOOL):
    _windows()
    function = getattr(ctypes.WinDLL(library, use_last_error=True), name)
    function.argtypes, function.restype = arguments, result
    return function


def _same_path(first, second):
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


def _binary(binary):
    binary = Path(binary)
    try:
        resolved = binary.resolve()
        unstable = {"chrome beta", "chrome dev", "chrome sxs", "chromium"}
        if (binary.name.lower() != "chrome.exe" or resolved.name.lower() != "chrome.exe"
                or any(part.lower() in unstable for part in resolved.parts) or not resolved.is_file()):
            raise ChromeError("chrome_not_found", f"不是存在的稳定版 chrome.exe：{binary}")
        return resolved
    except OSError as exc:
        raise ChromeError("chrome_lookup_failed", f"无法检查 Chrome 安装：{exc}") from exc


def _registry_candidates():
    import winreg

    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    value, kind = winreg.QueryValueEx(handle, None)
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        yield Path(os.path.expandvars(value.strip().strip('"')))
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ChromeError("chrome_lookup_failed", f"无法读取 Chrome App Paths：{exc}") from exc


def find_chrome() -> Path:
    _windows()
    candidates = list(_registry_candidates())
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Google/Chrome/Application/chrome.exe")
    for candidate in candidates:
        if any(part.lower() in {"chrome beta", "chrome dev", "chrome sxs", "chromium"} for part in candidate.parts):
            continue
        try:
            return _binary(candidate)
        except ChromeError as exc:
            if exc.code != "chrome_not_found":
                raise
    raise ChromeError("chrome_not_found", "未找到 Google Chrome 稳定版；不会使用 Edge 或其他浏览器。")


def profile_root() -> Path:
    _windows()
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise ChromeError("profile_not_found", "LOCALAPPDATA 未设置为绝对路径，无法定位 Chrome 用户目录。")
    return Path(local) / "Google/Chrome/User Data"


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("name", w.WCHAR * 260)]


def _chrome_pids():
    handle = _api("kernel32", "CreateToolhelp32Snapshot", (w.DWORD, w.DWORD), w.HANDLE)(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise _win_error("枚举进程")
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        arguments = (w.HANDLE, ctypes.POINTER(_ProcessEntry))
        found = _api("kernel32", "Process32FirstW", arguments)(handle, ctypes.byref(entry))
        pids = []
        while found:
            if entry.name.lower() == "chrome.exe":
                pids.append(entry.pid)
            found = _api("kernel32", "Process32NextW", arguments)(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise _win_error("枚举进程")
        return pids
    finally:
        _api("kernel32", "CloseHandle", (w.HANDLE,))(handle)


def _process_identity(pid):
    handle = _api("kernel32", "OpenProcess", (w.DWORD, w.BOOL, w.DWORD), w.HANDLE)(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise _win_error(f"读取进程 {pid}")
    try:
        size = w.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        arguments = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        if not _api("kernel32", "QueryFullProcessImageNameW", arguments)(handle, 0, name, ctypes.byref(size)):
            raise _win_error(f"读取进程 {pid} 路径")
        stamps = [w.FILETIME() for _ in range(4)]
        arguments = (w.HANDLE,) + (ctypes.POINTER(w.FILETIME),) * 4
        if not _api("kernel32", "GetProcessTimes", arguments)(handle, *(ctypes.byref(stamp) for stamp in stamps)):
            raise _win_error(f"读取进程 {pid} 创建时间")
        started = (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
        return Path(name.value), started
    finally:
        _api("kernel32", "CloseHandle", (w.HANDLE,))(handle)


def chrome_running(binary: Path) -> bool:
    _windows()
    binary = _binary(binary)
    for pid in _chrome_pids():
        identity = _process_identity(pid)
        if identity and _same_path(identity[0], binary):
            return True
    return False


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in ("state", "address", "port", "remote_address", "remote_port", "pid")]


def _listener_pids(port):
    arguments = (w.LPVOID, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG)
    query = _api("iphlpapi", "GetExtendedTcpTable", arguments, w.DWORD)
    size = w.DWORD(0)
    status = query(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    for _ in range(3):
        if status not in (0, 122):
            raise _win_error("查询 TCP 监听端口", status)
        if not 4 <= size.value <= 16 * 1024 * 1024:
            raise ChromeError("process_query_failed", "TCP 监听表大小异常。")
        buffer = ctypes.create_string_buffer(size.value)
        status = query(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if status != 0:
            continue
        count = w.DWORD.from_buffer_copy(buffer).value
        if 4 + count * ctypes.sizeof(_TcpRow) > len(buffer):
            raise ChromeError("process_query_failed", "TCP 监听表内容不完整。")
        owners = set()
        for index in range(count):
            row = _TcpRow.from_buffer_copy(buffer, 4 + index * ctypes.sizeof(_TcpRow))
            address = socket.inet_ntoa(int(row.address).to_bytes(4, "little"))
            if row.state == 2 and socket.ntohs(row.port & 0xFFFF) == port and address in ("127.0.0.1", "0.0.0.0"):
                owners.add(row.pid)
        return owners
    raise _win_error("查询 TCP 监听端口", status)


def _read_endpoint(root):
    try:
        with (Path(root) / "DevToolsActivePort").open("rb") as source:
            if os.fstat(source.fileno()).st_size > 4096:
                raise ChromeError("invalid_endpoint", "DevToolsActivePort 超过 4KB。")
            content = source.read(4096)
            if os.fstat(source.fileno()).st_size > 4096:
                raise ChromeError("invalid_endpoint", "DevToolsActivePort 超过 4KB。")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ChromeError("endpoint_read_failed", f"无法读取 DevToolsActivePort：{exc}") from exc
    match = re.fullmatch(rb"([0-9]{1,5})\r?\n(/devtools/browser/[A-Za-z0-9_-]{1,128})(?:\r?\n)?", content)
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ChromeError("invalid_endpoint", "DevToolsActivePort 的端口或浏览器路径非法。")
    return int(match[1]), match[2].decode("ascii")


def probe_endpoint(binary: Path, root: Path) -> dict | None:
    _windows()
    binary = _binary(binary)
    endpoint = _read_endpoint(root)
    if endpoint is None:
        return None
    port, path = endpoint
    owners = _listener_pids(port)
    if not owners:
        return None
    if len(owners) != 1 or 0 in owners:
        raise ChromeError("endpoint_mismatch", "Chrome 端口没有唯一有效的所属进程。")
    pid = next(iter(owners))
    identity = _process_identity(pid)
    if identity is None:
        return None
    if not _same_path(identity[0], binary):
        raise ChromeError("endpoint_mismatch", "DevToolsActivePort 指向其他进程；拒绝连接。")
    if _listener_pids(port) != owners:
        raise ChromeError("endpoint_mismatch", "验证期间 Chrome 端口所属进程发生变化。")
    return {"ws_url": f"ws://127.0.0.1:{port}{path}", "port": port, "pid": pid, "started": identity[1]}


def _last_profile(root):
    try:
        with (Path(root) / "Local State").open("rb") as source:
            if os.fstat(source.fileno()).st_size > 1024 * 1024:
                return None
            data = json.loads(source.read(1024 * 1024))
        name = data.get("profile", {}).get("last_used")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,127}", name) or name.endswith(" "):
            return None
        folder = Path(root) / name
        if folder.is_dir() and _same_path(folder.resolve().parent, root):
            return name
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        pass
    return None


def prepare_chrome(binary: Path, root: Path, wait_seconds: float = 10) -> dict:
    _windows()
    binary = _binary(binary)
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise ChromeError("invalid_argument", "wait_seconds 必须是有限的非负秒数。")
    deadline = time.monotonic() + wait_seconds
    endpoint = probe_endpoint(binary, root)
    if endpoint:
        return endpoint
    message = "请在 Chrome 打开 chrome://inspect/#remote-debugging 并允许远程调试，然后重试；不会关闭或重启浏览器。"
    if chrome_running(binary):
        raise ChromeError("setup_required", message)
    arguments = [str(binary)]
    selected = _last_profile(root)
    if selected:
        arguments.append(f"--profile-directory={selected}")
    try:
        subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ChromeError("launch_failed", f"无法启动 Chrome：{exc}") from exc
    while time.monotonic() < deadline:
        endpoint = probe_endpoint(binary, root)
        if endpoint:
            return endpoint
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.2, remaining))
    raise ChromeError("setup_required", message)
