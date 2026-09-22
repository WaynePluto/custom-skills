"""复用官方 daemon/CLI，绑定 Chrome 端点并提供只读专用页身份查询。"""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

# -I 隔离调用者的 Python 环境，再显式导入本技能的宿主模块。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chrome_host import ChromeError, chrome_running, find_chrome, prepare_chrome, probe_endpoint, profile_root
from chrome_session import SESSION_TAB_META, SESSION_TAB_PROTOCOL, read_session_tab, session_tab_reply

ADAPTER = "chrome-entry-v1"
NAME = "chrome"
APPROVAL_TIMEOUT = 180
OUTPUT_LIMIT = 12000


def check_version():
    current = version("browser-harness")
    supported = json.loads(Path(__file__).with_name("compatibility.json").read_text(encoding="utf-8"))["browser_harness_versions"]
    if current not in supported:
        raise ChromeError("incompatible_version", f"browser-harness {current} 未经适配验证；支持 {', '.join(supported)}。请更新适配器，不会回退到自动浏览器发现。")
    return current


def runtime_dir():
    raw = os.environ.get("BH_RUNTIME_DIR")
    if not raw or os.environ.get("BU_NAME") != NAME:
        raise ChromeError("invalid_entry", "请通过 chrome.py 调用，不直接运行内部模块。")
    root = Path(raw)
    root.mkdir(parents=True, exist_ok=True)
    return root


def binding(endpoint):
    # 不在状态输出中暴露可控制浏览器的 WebSocket 地址。
    payload = json.dumps(endpoint, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def read_state(root):
    path = root / "chrome-entry.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("adapter") != ADAPTER
                or not isinstance(data.get("binding"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", data["binding"])
                or data.get("phase") not in {"starting", "ready", "failed"}
                or not isinstance(data.get("generation"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", data["generation"])):
            raise ValueError("invalid adapter state")
        owner = data.get("owner")
        if owner is not None and (not isinstance(owner, dict) or type(owner.get("pid")) is not int
                                  or not 0 < owner["pid"] < 2**31
                                  or type(owner.get("started")) is not int or owner["started"] <= 0):
            raise ValueError("invalid owner")
        if data["phase"] == "ready" and owner is None:
            raise ValueError("missing owner")
        return data
    except (OSError, ValueError, AttributeError) as exc:
        raise ChromeError("invalid_state", "本 session 状态不可验证；不会接管或停止未知进程。") from exc


def write_state(root, state):
    temporary = root / f"chrome-entry.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(root / "chrome-entry.json")


def daemon_request(ipc, request):
    connection, token = ipc.connect(NAME, timeout=3)
    try:
        return ipc.request(connection, token, request)
    finally:
        connection.close()


def verify_owner(state, alive, pending, admin, ipc):
    """Chrome 绑定与 daemon 身份分别校验，不能拿旧状态接管新进程。"""
    owner = state.get("owner") if state else None
    if not owner or admin._process_start_time(owner["pid"]) != owner["started"]:
        raise ChromeError("unknown_daemon", "daemon 进程归属不可验证；拒绝接管或停止。")
    if alive:
        response = daemon_request(ipc, {"meta": "ping"})
        expected = {"adapter": ADAPTER, "generation": state["generation"],
                    "binding": state["binding"], "pid": owner["pid"], "pong": True}
        if not isinstance(response, dict) or any(response.get(key) != value for key, value in expected.items()):
            raise ChromeError("unknown_daemon", "IPC 身份与本 session 记录不符；拒绝接管或停止。")
        return response
    elif pending != owner["pid"]:
        raise ChromeError("unknown_daemon", "等待授权的进程与本 session 记录不符。")


def stop_session(root, admin, ipc):
    state = read_state(root)
    alive = admin.daemon_alive(NAME)
    pending = admin._fingerprinted_pending_pid(ipc.pid_path(NAME))
    if alive or pending:
        verify_owner(state, alive, pending, admin, ipc)
        if alive:
            response = daemon_request(ipc, {"meta": "shutdown"})
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise ChromeError("stop_failed", "daemon 未确认关闭；保留状态，不强杀。")
            deadline = time.monotonic() + 6
            owner = state["owner"]
            # 不使用 os.kill(pid, 0)：Windows 会将其解释为终止进程。
            while admin._process_start_time(owner["pid"]) == owner["started"]:
                if time.monotonic() >= deadline:
                    raise ChromeError("stop_failed", "daemon 正在清理，请稍后重试 --stop；不强杀。")
                time.sleep(0.1)
        else:
            try:
                admin.restart_daemon(NAME)
            except PermissionError:
                pass
        if admin.daemon_alive(NAME) or admin._fingerprinted_pending_pid(ipc.pid_path(NAME)):
            raise ChromeError("stop_failed", "本 session 的 daemon 尚未退出；未操作 Chrome。")
    elif state and state.get("owner") and admin._process_start_time(state["owner"]["pid"]) == state["owner"]["started"]:
        raise ChromeError("stop_failed", "本 session 进程仍在运行但 IPC 不可用；保留状态，稍后重试。")
    deadline = time.monotonic() + 2
    while True:
        try:
            ipc.cleanup_endpoint(NAME)
            ipc.pid_path(NAME).unlink(missing_ok=True)
            (root / "chrome-entry.json").unlink(missing_ok=True)
            break
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise ChromeError("cleanup_pending", "daemon 已停止但状态文件仍被占用；稍后重试 --stop，不会重启浏览器。") from exc
            time.sleep(0.1)
    return {"ok": True, "status": "stopped", "message": "只停止本 session 的 daemon；未关闭 Chrome 或其他浏览器。"}


def ensure_connection(root, endpoint, admin, ipc):
    expected = binding(endpoint)
    state = read_state(root)
    alive = admin.daemon_alive(NAME)
    pending = admin._fingerprinted_pending_pid(ipc.pid_path(NAME))
    if alive or pending:
        verify_owner(state, alive, pending, admin, ipc)
        if state["binding"] != expected:
            raise ChromeError("endpoint_changed", "Chrome 进程或端点已改变；请先用同一 session 执行 --stop，再 --ensure。")
        if alive:
            try:
                admin.require_existing_daemon(NAME)
            except RuntimeError as exc:
                raise ChromeError("connection_lost", "Chrome 连接已断开；请用同一 session 执行 --stop，再 --ensure。") from exc
            return {"ok": True, "status": "ready"}
        if state["phase"] == "ready":
            raise ChromeError("connection_lost", "原 daemon 的 IPC 不可用；请先 --stop，不自动建立第二个连接。")
        return {"ok": False, "status": "approval_pending", "message": "请确认 Chrome 当前的远程调试授权弹窗，然后用同一 session 重试。不会创建第二个连接；180 秒未完成则释放连接。"}
    if state and state.get("owner") and admin._process_start_time(state["owner"]["pid"]) == state["owner"]["started"]:
        raise ChromeError("connection_lost", "原 daemon 进程仍在运行；保留状态，不重复启动。")
    if state and state.get("phase") != "ready":
        raise ChromeError("connection_failed", "上次连接未完成或授权被拒绝/超时；请确认后用同一 session 执行 --stop，再 --ensure。")
    state = {"adapter": ADAPTER, "binding": expected, "phase": "starting",
             "generation": secrets.token_hex(16), "owner": None}
    write_state(root, state)
    # 不使用官方 ensure_daemon 的跨浏览器扫描和失效重试分支。
    with ipc.log_path(NAME).open("w", encoding="utf-8") as log:
        log.write("handshake-wait: Chrome 专用连接正在准备，可能需要用户授权\n")
    with ipc.log_path(NAME).open("a", encoding="utf-8") as log:
        child = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "daemon"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=log, env=os.environ.copy(), **ipc.spawn_kwargs())
    admin._publish_pid(ipc.pid_path(NAME), child.pid)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if admin.daemon_alive(NAME):
            verify_owner(read_state(root), True, None, admin, ipc)
            admin.require_existing_daemon(NAME)
            return {"ok": True, "status": "ready"}
        if child.poll() is not None:
            raise ChromeError("connection_failed", "Chrome 握手失败；请检查调试授权。确认后用同一 session 执行 --stop，再 --ensure；不自动重连。")
        time.sleep(0.1)
    return {"ok": False, "status": "approval_pending", "message": "请在 Chrome 中允许当前远程调试连接，再用同一 session 重试。等待有 180 秒上限；不会反复弹出授权。"}


async def serve_pinned(root, state, daemon):
    """保留官方本机授权握手，不让显式 CDP 模式把它当作远程连接。"""
    endpoint = probe_endpoint(find_chrome(), profile_root())
    if not endpoint or binding(endpoint) != state["binding"]:
        raise ChromeError("endpoint_changed", "启动期间 Chrome 端点发生变化。")
    daemon.get_ws_url = lambda: endpoint["ws_url"]
    daemon.PROFILES = [profile_root()]
    daemon.BROWSER_KIND = "local"
    daemon.REMOTE_ID = None

    class PinnedDaemon(daemon.Daemon):
        async def handle(self, request):
            meta = request.get("meta")
            if meta not in ("ping", SESSION_TAB_META):
                method = request.get("method")
                if isinstance(method, str) and not method.startswith("Target.") and not request.get("session_id"):
                    # 上游会为隐式 stale session 新建页面并重放动作；固定会话使关闭竞态失败停止。
                    if not self.session:
                        return {"error": "not_attached"}
                    request = {**request, "session_id": self.session}
                return await super().handle(request)
            # 私有 meta 也先经过上游 token 校验，不能直接绕过 Daemon.handle。
            response = await super().handle({**request, "meta": "ping"})
            if response.get("pong") is not True:
                return response
            response.update(adapter=ADAPTER, generation=state["generation"], binding=state["binding"],
                            session_tab_protocol=SESSION_TAB_PROTOCOL)
            if meta == SESSION_TAB_META:
                return await session_tab_reply(self, request, response)
            return response

        async def _close_inspect_tabs(self, targets):
            # 用户原有的 inspect 页也属于用户资源，不执行上游批量关闭逻辑。
            return

    connection = PinnedDaemon()
    try:
        await asyncio.wait_for(connection.start(), APPROVAL_TIMEOUT)
        write_state(root, {**state, "phase": "ready"})
        await daemon.serve(connection)
    finally:
        if connection.cdp:
            if connection.dedicated_target_id:
                try:
                    await asyncio.wait_for(connection.cdp.send_raw(
                        "Target.closeTarget", {"targetId": connection.dedicated_target_id}), 2)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(connection.cdp.stop(), 2)
            except Exception:
                pass


def daemon_main(root, admin, ipc):
    from browser_harness import daemon

    state = read_state(root)
    if not state or admin.daemon_alive(NAME):
        return 1
    # 父子均发布带创建时间的记录，避免上游裸 PID 写入覆盖指纹。
    admin._publish_pid(ipc.pid_path(NAME), os.getpid())
    started = admin._process_start_time(os.getpid())
    if started is None:
        return 1
    state = {**state, "owner": {"pid": os.getpid(), "started": started}}
    write_state(root, state)
    try:
        asyncio.run(serve_pinned(root, state, daemon))
        return 0
    except Exception as exc:
        current = read_state(root)
        if current and current["generation"] == state["generation"]:
            write_state(root, {**state, "phase": "failed"})
        # 日志只记录分类，不泄漏 WS 地址或浏览器返回的页面内容。
        daemon.log(f"connection-failed: {type(exc).__name__}")
        return 1
    finally:
        if admin._pid_number(ipc.pid_path(NAME)) == os.getpid():
            ipc.pid_path(NAME).unlink(missing_ok=True)
            ipc.cleanup_endpoint(NAME)


class BoundedOutput(io.TextIOBase):
    """限制送回模型的脚本输出，不缓存无限长字符串。"""
    def __init__(self):
        self.parts = []
        self.length = 0
        self.truncated = False

    def write(self, text):
        count = len(text)
        remaining = OUTPUT_LIMIT - self.length
        if remaining and text:
            self.parts.append(text[:remaining])
        self.length += min(count, remaining)
        self.truncated |= count > remaining
        return count

    def value(self):
        return "".join(self.parts)


def session_tab(root, admin, ipc):
    """先核验持有者，再读取明确专用页；不信任当前页或任意空白页。"""
    identity = verify_owner(read_state(root), True, None, admin, ipc)
    return read_session_tab(identity, lambda request: daemon_request(ipc, request))


def execute_script(code, admin):
    from browser_harness import run, _ipc as ipc

    admin.require_existing_daemon(NAME)
    # 只向当前进程的官方脚本 globals 注入本地只读 helper，不修改第三方文件。
    run.session_tab = lambda: session_tab(runtime_dir(), admin, ipc)
    # 保留官方 CLI 的 helpers/exec；不调用其升级联网提示或遥测入口。
    run.print_update_banner = lambda: None
    sys.stdin = io.StringIO(code)
    output = BoundedOutput()
    status = "completed"
    error_code = None
    with redirect_stdout(output), redirect_stderr(output):
        try:
            run._run([])
        except BaseException as exc:
            if isinstance(exc, SystemExit) and exc.code in (None, 0):
                pass
            else:
                status = "script_failed"
                error_code = exc.code if isinstance(exc, ChromeError) else None
                label = f" [{error_code}]" if error_code else ""
                output.write(f"\n{type(exc).__name__}{label}: {exc}")
    result = {"ok": status == "completed", "status": status, "output": output.value(), "truncated": output.truncated}
    if error_code:
        result["error_code"] = error_code
    return result


def dispatch(action, root, admin, ipc):
    if action == "stop":
        return stop_session(root, admin, ipc)
    binary, profile = find_chrome(), profile_root()
    if action == "doctor":
        endpoint = probe_endpoint(binary, profile)
        state = read_state(root)
        alive = admin.daemon_alive(NAME)
        pending = admin._fingerprinted_pending_pid(ipc.pid_path(NAME))
        status = "chrome_closed" if not chrome_running(binary) else "setup_required" if not endpoint else "daemon_idle"
        if alive or pending:
            if not state or not endpoint or state["binding"] != binding(endpoint):
                status = "endpoint_changed"
            else:
                try:
                    verify_owner(state, alive, pending, admin, ipc)
                    if alive:
                        admin.require_existing_daemon(NAME)
                    status = "ready" if alive else "approval_pending"
                except ChromeError:
                    status = "unknown_daemon"
                except RuntimeError:
                    status = "connection_lost"
        elif state and state.get("phase") == "failed":
            status = "connection_failed"
        return {"ok": status == "ready", "status": status, "chrome": str(binary),
                "profile_root": str(profile), "endpoint_verified": bool(endpoint),
                "daemon_alive": alive, "version": check_version()}
    endpoint = prepare_chrome(binary, profile)
    result = ensure_connection(root, endpoint, admin, ipc)
    if action == "run" and result["ok"]:
        # 执行前复核进程指纹，避免启动和使用之间被用户替换浏览器。
        if probe_endpoint(binary, profile) != endpoint:
            raise ChromeError("endpoint_changed", "Chrome 端点在执行前改变；本次未执行脚本。")
        return execute_script(sys.stdin.read(65537), admin)
    return result


def main(action):
    try:
        check_version()
        root = runtime_dir()
        from browser_harness import admin, _ipc as ipc
        if action == "daemon":
            return daemon_main(root, admin, ipc)
        # 使用独立的入口锁；官方停止 pending daemon 时仍可获取其 spawn 锁。
        with admin._spawn_lock(NAME + "-entry", timeout=2) as lock:
            if lock.fd is None:
                raise ChromeError("session_busy", "同一 session 正在执行其他任务；不要并行操作同一会话。")
            result = dispatch(action, root, admin, ipc)
    except Exception as exc:
        result = {"ok": False, "status": getattr(exc, "code", "runtime_error"), "message": str(exc)[:1500]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
