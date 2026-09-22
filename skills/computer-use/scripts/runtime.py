#!/usr/bin/env python3
"""一次性 desktop worker：读取一个 JSON，在全局锁内路由并持久化。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# supervisor 使用 `-I`；只恢复本技能自己的受审计模块目录。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from backend import CuaBackend
from driver_bridge import BACKEND_ID
from contracts import (
    ACTION_COMMANDS,
    MAX_REQUEST_BYTES,
    MAX_TREE_ELEMENTS,
    ToolError,
    elapsed_ms,
    error_result,
    finite_number,
    integer,
    normalize_shortcut,
    require,
    result,
    validate_point,
    validate_ref,
    validate_request,
    validate_text,
)
from guard import (
    DesktopMutex,
    element_center,
    image_to_screen,
    relocate_element,
    point_in_box,
    same_topology,
    validate_target_point,
)
import state


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """移除仅供重验证的内部映射。"""
    public = dict(snapshot)
    public.pop("window_identity", None)
    public.pop("session_generation", None)
    public.pop("action_generation", None)
    public.pop("consumed", None)
    elements = public.get("elements", [])
    if isinstance(elements, dict):
        public["elements"] = list(elements.values())
    return public


def _current_bound_window(backend: CuaBackend, session: str, window_ref: str,
                          *, geometry: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    saved = state.get_window(session, validate_ref(window_ref, "window"))
    current = backend.validate_window(saved, require_geometry=geometry)
    return saved, current


def _save_tree_snapshot(backend: CuaBackend, session: str, window_ref: str,
                        current: dict[str, Any], max_elements: int) -> dict[str, Any]:
    captured = backend.snapshot(current["hwnd"], max_elements=max_elements)
    captured["window_identity"] = current
    saved = state.save_snapshot(session, window_ref, captured)
    return saved


def _finish_action_error(session: str, request_id: str, error: ToolError,
                         started: float, dispatched: bool) -> dict[str, Any]:
    if dispatched and error.side_effect == "none":
        error = ToolError(error.code, error.message, side_effect="uncertain", details=error.details)
    payload = error_result(error, session=session, request_id=request_id, elapsed_ms=elapsed_ms(started))
    if dispatched:
        try:
            state.complete_request(session, request_id, payload)
        except Exception:
            # 状态盘满或损坏不能覆盖已经发生的副作用，也不能阻止返回保守结果。
            payload["side_effect"] = "uncertain"
    return payload


def _handle_focus(request: dict[str, Any], backend: CuaBackend, started: float) -> dict[str, Any]:
    session = request["session"]
    request_id = request["request_id"]
    args = request["args"]
    fingerprint = state.request_fingerprint(session, request)
    duplicate = state.get_request_result(session, request_id, fingerprint)
    if duplicate is not None:
        return duplicate
    window_ref = validate_ref(args.get("window"), "window")
    max_elements = integer(args.get("max_elements", 200), "max-elements", 1, MAX_TREE_ELEMENTS)
    dispatched = False
    try:
        _saved, current = _current_bound_window(backend, session, window_ref)
        state.commit_dispatch(session, request_id, "focus", fingerprint=fingerprint)
        dispatched = True
        focused = backend.focus(current["hwnd"])
        snapshot = _save_tree_snapshot(backend, session, window_ref, focused, max_elements)
        payload = result(ok=True, status="dispatched", session=session, request_id=request_id,
                         side_effect="dispatched", verified=True,
                         data={"window": focused, "snapshot": _public_snapshot(snapshot),
                               "verification": {"foreground_hwnd": focused["hwnd"]}},
                         elapsed_ms=elapsed_ms(started))
        state.complete_request(session, request_id, payload)
        return payload
    except ToolError as error:
        return _finish_action_error(session, request_id, error, started, dispatched)
    except Exception as exc:
        error = ToolError("execution_error", "聚焦执行失败。", side_effect="uncertain" if dispatched else "none",
                          details={"exception": type(exc).__name__})
        return _finish_action_error(session, request_id, error, started, dispatched)


def _fresh_candidates(backend: CuaBackend, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(snapshot.get("element_limit") or 200)
    fresh = backend.snapshot(int(snapshot["window_identity"]["hwnd"]), max_elements=min(limit, MAX_TREE_ELEMENTS))
    return fresh["elements"]


def _resolve_action_point(args: dict[str, Any], snapshot: dict[str, Any],
                          candidates: list[dict[str, Any]] | None) -> tuple[list[int], dict[str, Any] | None]:
    element_ref = args.get("element")
    point = args.get("point")
    require((element_ref is None) != (point is None), "invalid_argument",
            "必须且只能指定 element 或 point。")
    if element_ref is not None:
        require(candidates is not None, "invalid_state", "元素动作缺少重采样结果。")
        saved_element = state.get_element(snapshot, validate_ref(element_ref, "element"))
        relocated = relocate_element(saved_element, candidates)
        return element_center(relocated), relocated
    require(args.get("space") == "image", "invalid_argument", "point 仅支持 --space image。")
    require(snapshot.get("screenshot", {}).get("coordinates_usable") is True,
            "unsupported_action", "SDK 未提供可验证的图片坐标映射，请使用新快照中的元素目标。")
    require(snapshot.get("foreground_hwnd") == snapshot.get("window_identity", {}).get("hwnd"),
            "invalid_state", "截图采集时目标窗口不在前台，不能用于坐标动作。")
    require(args.get("window") == snapshot.get("window_ref"), "invalid_argument",
            "坐标动作必须显式绑定 snapshot 所属 window。")
    return image_to_screen(validate_point(point), snapshot.get("screenshot")), None


def _preflight_snapshot(request: dict[str, Any], backend: CuaBackend) -> tuple[dict[str, Any], dict[str, Any]]:
    session = request["session"]
    args = request["args"]
    snapshot_ref = validate_ref(args.get("snapshot"), "snapshot")
    snapshot = state.get_snapshot(session, snapshot_ref, for_action=True)
    require(snapshot.get("backend_id") == BACKEND_ID, "invalid_state", "快照不属于当前驱动版本，请 cleanup 后重新观察。")
    if args.get("window") is not None:
        require(args["window"] == snapshot.get("window_ref"), "invalid_argument",
                "window 与 snapshot 归属不一致。")
    current = backend.validate_window(snapshot["window_identity"], require_geometry=True)
    if args.get("delivery", "background") == "foreground":
        backend.require_foreground(current["hwnd"])
    require(same_topology(snapshot.get("topology"), backend.topology()),
            "target_changed", "显示器拓扑、DPI 或缩放已变化，请重新观察。")
    return snapshot, current


def _require_action_budget(request: dict[str, Any], required_seconds: float,
                           action: str) -> None:
    """确认 supervisor 总期限足以覆盖即将下发的长动作。"""
    deadline = request.get("deadline_monotonic")
    require(isinstance(deadline, (int, float)) and not isinstance(deadline, bool),
            "invalid_argument", f"{action} 请求缺少 supervisor 总期限。")
    remaining = float(deadline) - time.monotonic()
    if remaining < required_seconds:
        raise ToolError(
            "timeout",
            f"剩余预算不足以安全完成 {action}；未下发桌面动作。",
            details={"remaining_ms": max(0, int(remaining * 1000)),
                     "required_ms": int(required_seconds * 1000)},
        )


def _require_type_budget(request: dict[str, Any]) -> None:
    """为 native 写入及后观察预留预算，不再按旧驱动逐字符复核估算。"""
    text = validate_text(request.get("input_text"))
    estimated = 2.0 + len(text.encode("utf-16-le")) * 0.005
    _require_action_budget(request, estimated, "type")


def _handle_input(request: dict[str, Any], backend: CuaBackend, started: float) -> dict[str, Any]:
    session = request["session"]
    request_id = request["request_id"]
    command = request["command"]
    args = request["args"]
    fingerprint = state.request_fingerprint(session, request)
    duplicate = state.get_request_result(session, request_id, fingerprint)
    if duplicate is not None:
        return duplicate
    dispatched = False
    snapshot_ref: str | None = None
    try:
        snapshot, current = _preflight_snapshot(request, backend)
        snapshot_ref = snapshot["snapshot_ref"]
        needs_tree = command in {"click", "type", "scroll", "move"} and args.get("element") is not None
        needs_tree = needs_tree or (command == "drag" and
                                    (args.get("from_element") is not None or args.get("to_element") is not None))
        candidates = _fresh_candidates(backend, snapshot) if needs_tree else None

        point = relocated = source = target = None
        if command in {"click", "type", "scroll", "move"}:
            point, relocated = _resolve_action_point(args, snapshot, candidates)
            require(point_in_box(point, current["bounding_box"]), "target_changed", "元素中心位于目标窗口之外。")
            if args.get("delivery", "background") == "foreground":
                validate_target_point(point, current, backend)
            if command == "type":
                require(relocated is not None, "invalid_argument", "type 只支持元素目标。")
                backend.validate_text_target(point, relocated, current["hwnd"])
        elif command == "shortcut":
            require(args.get("window") == snapshot.get("window_ref"), "invalid_argument",
                    "shortcut 必须显式绑定 snapshot 所属 window。")
            args["keys"] = normalize_shortcut(args.get("keys"))
        elif command == "drag":
            source_args = {"element": args.get("from_element"), "point": args.get("from_point"),
                           "space": args.get("space"), "window": args.get("window")}
            target_args = {"element": args.get("to_element"), "point": args.get("to_point"),
                           "space": args.get("space"), "window": args.get("window")}
            source, _ = _resolve_action_point(source_args, snapshot, candidates)
            target, _ = _resolve_action_point(target_args, snapshot, candidates)
            validate_target_point(source, current, backend)
            validate_target_point(target, current, backend)
            duration = finite_number(args.get("duration", 0.5), "duration", 0, 10)
        else:
            raise ToolError("invalid_argument", "未知输入动作。")

        backend.preflight_action(command, current, relocated, args)
        # UIA 重采样本身可能耗时；提交 dispatch 前再次复核易变前置条件。
        current = backend.validate_window(snapshot["window_identity"], require_geometry=True)
        if args.get("delivery", "background") == "foreground":
            backend.require_foreground(current["hwnd"])
        require(same_topology(snapshot.get("topology"), backend.topology()),
                "target_changed", "显示器拓扑、DPI 或缩放已变化，请重新观察。")
        if args.get("delivery", "background") == "foreground":
            if command in {"click", "type", "scroll", "move"}:
                validate_target_point(point, current, backend)
            elif command == "drag":
                validate_target_point(source, current, backend)
                validate_target_point(target, current, backend)

        if command == "type":
            _require_type_budget(request)
        elif command == "drag":
            _require_action_budget(request, duration + 2.0, "drag")
        before = backend.minimal_observation(current["hwnd"])
        state.commit_dispatch(session, request_id, command, snapshot_ref,
                              fingerprint=fingerprint)
        dispatched = True
        verdict = backend.execute_action(command, current, args, point=point, element=relocated,
                                         source=source, target=target, text=request.get("input_text"))
        observation = backend.minimal_observation(current["hwnd"])
        if args.get("delivery", "background") == "background" and (
                before["foreground_hwnd"] != observation["foreground_hwnd"] or
                before["cursor"] != observation["cursor"]):
            raise ToolError("interference_detected", "后台动作期间焦点或鼠标变化；可能是用户输入，停止且不恢复或重放。",
                            side_effect="uncertain", details={"before": before, "after": observation})
        payload = result(ok=True, status="dispatched", session=session, request_id=request_id,
                         side_effect="dispatched", verified=False,
                         data={"action": command, "snapshot_consumed": snapshot_ref,
                               "observation": observation, "before": before, "driver_verdict": verdict,
                               "delivery": args.get("delivery", "background"),
                               **({"input_length": len(request["input_text"])} if command == "type" else {})},
                         elapsed_ms=elapsed_ms(started))
        state.complete_request(session, request_id, payload)
        return payload
    except ToolError as error:
        return _finish_action_error(session, request_id, error, started, dispatched)
    except Exception as exc:
        error = ToolError("execution_error", "桌面输入执行失败。",
                          side_effect="uncertain" if dispatched else "none",
                          details={"exception": type(exc).__name__})
        return _finish_action_error(session, request_id, error, started, dispatched)


def _wait(request: dict[str, Any], backend: CuaBackend, started: float) -> dict[str, Any]:
    session = request["session"]
    args = request["args"]
    window_ref = validate_ref(args.get("window"), "window")
    _saved, current = _current_bound_window(backend, session, window_ref)
    condition = args.get("condition", "foreground")
    require(condition in {"foreground", "appears", "disappears", "text"},
            "invalid_argument", "wait condition 无效。")
    wait_seconds = finite_number(args.get("wait_seconds", 10), "wait-seconds", 0.1, 120)
    max_elements = integer(args.get("max_elements", 200), "max-elements", 1, MAX_TREE_ELEMENTS)
    deadline = time.monotonic() + wait_seconds
    last_snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        current = backend.validate_window(current)
        if condition == "foreground":
            if backend.foreground_hwnd() == current["hwnd"]:
                captured = _save_tree_snapshot(backend, session, window_ref, current, max_elements)
                return result(ok=True, status="condition_met", session=session,
                              data={"condition": condition, "snapshot": _public_snapshot(captured)},
                              verified=True, elapsed_ms=elapsed_ms(started))
        else:
            captured = backend.snapshot(current["hwnd"], max_elements=max_elements)
            last_snapshot = captured
            name = args.get("name")
            control_type = args.get("control_type")
            require(isinstance(name, str) and 0 < len(name) <= 512,
                    "invalid_argument", "元素条件必须提供 name。")
            matches = [item for item in captured["elements"]
                       if item.get("name") == name and
                       (control_type is None or item.get("control_type") == control_type)]
            if len(matches) > 1:
                raise ToolError("ambiguous_target", "wait 元素条件匹配到多个候选。",
                                details={"candidate_count": len(matches)})
            met = condition == "appears" and len(matches) == 1
            if condition == "disappears":
                met = len(matches) == 0 and captured.get("tree_complete") is True
            if condition == "text" and len(matches) == 1:
                expected = args.get("text")
                require(isinstance(expected, str) and len(expected) <= 512,
                        "invalid_argument", "text 条件必须提供不超过 512 字符的文本。")
                met = backend.text_value(current["hwnd"], matches[0]) == expected
            if met:
                captured["window_identity"] = current
                saved_snapshot = state.save_snapshot(session, window_ref, captured)
                return result(ok=True, status="condition_met", session=session,
                              data={"condition": condition, "snapshot": _public_snapshot(saved_snapshot)},
                              verified=True, elapsed_ms=elapsed_ms(started))
        time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
    raise ToolError("verification_failed", "wait 条件在预算内未满足。",
                    details={"condition": condition, "had_complete_sample": bool(
                        last_snapshot and last_snapshot.get("tree_complete"))})


def handle(request: dict[str, Any], started: float) -> dict[str, Any]:
    validate_request(request)
    command = request["command"]
    session = request.get("session")
    request_id = request.get("request_id")
    if command == "doctor":
        data = CuaBackend.doctor()
        return result(ok=True, status="ready", data=data, elapsed_ms=elapsed_ms(started))

    with DesktopMutex():
        state.purge_expired()
        if command == "cleanup":
            data = state.cleanup_session(session)
            return result(ok=True, status="cleaned", session=session, data=data,
                          elapsed_ms=elapsed_ms(started))

        try:
            with CuaBackend() as backend:
                return _route(request, backend, started)
        except Exception as exc:
            # shutdown 也可能失败；已经留下 dispatch 意图时绝不能返回 none。
            if command in ACTION_COMMANDS:
                try:
                    dispatched = request_id in state.load_session(session, create=False).get("requests", {})
                except Exception:
                    dispatched = True
                if dispatched:
                    error = ToolError("execution_error", "动作执行或 SDK 关闭失败；不得重发。",
                                      side_effect="uncertain", details={"exception": type(exc).__name__})
                    return _finish_action_error(session, request_id, error, started, True)
            raise


def _route(request: dict[str, Any], backend: CuaBackend, started: float) -> dict[str, Any]:
    command, session = request["command"], request["session"]
    if command == "windows":
        windows = state.register_windows(session, backend.windows())
        return result(ok=True, status="observed", session=session,
                      data={"windows": windows, "count": len(windows)}, elapsed_ms=elapsed_ms(started))
    if command == "focus":
        return _handle_focus(request, backend, started)
    if command == "snapshot":
        args = request["args"]
        window_ref = validate_ref(args.get("window"), "window")
        limit = integer(args.get("max_elements", 200), "max-elements", 1, MAX_TREE_ELEMENTS)
        _saved, current = _current_bound_window(backend, session, window_ref)
        captured = _save_tree_snapshot(backend, session, window_ref, current, limit)
        return result(ok=True, status="observed", session=session,
                      data={"snapshot": _public_snapshot(captured)}, elapsed_ms=elapsed_ms(started))
    if command == "screenshot":
        return _handle_screenshot(request, backend, started)
    if command in {"click", "type", "shortcut", "scroll", "move", "drag"}:
        return _handle_input(request, backend, started)
    if command == "wait":
        return _wait(request, backend, started)
    raise ToolError("invalid_argument", "未知命令。")


def _handle_screenshot(request: dict[str, Any], backend: CuaBackend, started: float) -> dict[str, Any]:
    session, args = request["session"], request["args"]
    require(args.get("display") is None, "unsupported_action", "仅支持窗口截图，不自动扩大范围。")
    window_ref = validate_ref(args.get("window"), "window")
    _saved, current = _current_bound_window(backend, session, window_ref)
    destination = state.artifact_path(session)
    try:
        shot = backend.capture(destination, hwnd=current["hwnd"])
        state.validate_artifact_quota(session, destination)
        captured = {
            "backend_id": BACKEND_ID, "elements": [], "tree_complete": False, "tree_truncated": False,
            "element_limit": 0, "topology": backend.topology(), "foreground_hwnd": backend.foreground_hwnd(),
            "screenshot": shot, "window_identity": current,
        }
        saved = state.save_snapshot(session, window_ref, captured)
        return result(ok=True, status="observed", session=session,
                      data={"screenshot": shot, "snapshot": _public_snapshot(saved)},
                      artifacts=[{"path": shot["path"], "kind": "image/png"}], elapsed_ms=elapsed_ms(started))
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _verify_supervised_job() -> None:
    """确认当前 worker 确实属于 supervisor 创建的命名 Job Object。"""
    if sys.platform != "win32" or os.environ.get("COMPUTER_USE_SUPERVISED") != "1":
        raise ToolError("permission_denied", "runtime 只能由 computer.py supervisor 启动。")
    name = os.environ.get("COMPUTER_USE_JOB_NAME", "")
    if not name:
        raise ToolError("permission_denied", "缺少 supervisor Job Object 身份。")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                        ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    job = kernel32.OpenJobObjectW(0x0004, False, name)  # 查询 Job Object 的权限
    if not job:
        raise ToolError("permission_denied", "无法打开 supervisor Job Object。")
    try:
        belongs = wintypes.BOOL()
        ok = kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), job, ctypes.byref(belongs))
        if not ok or not belongs.value:
            raise ToolError("permission_denied", "worker 未加入指定的受监督进程树。")
    finally:
        kernel32.CloseHandle(job)


def main() -> int:
    started = time.monotonic()
    session = None
    request_id = None
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        require(0 < len(raw) <= MAX_REQUEST_BYTES, "invalid_argument", "worker 请求为空或超限。")
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError("invalid_argument", "worker 请求不是有效 UTF-8 JSON。") from exc
        if isinstance(request, dict):
            session = request.get("session")
            request_id = request.get("request_id")
        _verify_supervised_job()
        payload = handle(request, started)
    except ToolError as error:
        payload = error_result(error, session=session, request_id=request_id,
                               elapsed_ms=elapsed_ms(started))
    except Exception as exc:
        payload = error_result(
            ToolError("execution_error", "worker 未处理异常。",
                      details={"exception": type(exc).__name__}),
            session=session, request_id=request_id, elapsed_ms=elapsed_ms(started),
        )
    from contracts import dumps_result
    sys.stdout.write(dumps_result(payload) + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
