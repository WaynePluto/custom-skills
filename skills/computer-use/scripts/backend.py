#!/usr/bin/env python3
"""cua-driver 的唯一执行适配层；观察不聚焦，后台失败不降级。"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any

# 安装校验使用 -I，仅恢复本技能目录。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from contracts import MAX_TREE_ELEMENTS, ToolError, require
from driver_bridge import BACKEND_ID, EXPECTED_VERSION, DriverSession, enum_name, load_sdk


class CuaBackend:
    """驱动对象只在本次 worker 存活；上游 token 不跨进程执行。"""

    def __init__(self) -> None:
        from win32_guard import WindowsGuard
        self.sdk = load_sdk()
        self.guard = WindowsGuard()
        self.guard.desktop_available()
        self.bridge = DriverSession(self.sdk)

    def __enter__(self) -> "CuaBackend":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.bridge.close()

    @classmethod
    def doctor(cls) -> dict[str, Any]:
        # 验证 SDK 创建/关闭，但不截图、聚焦、输入或调用 MCP。
        with cls() as backend:
            metadata = backend.bridge.run(backend.bridge.driver.metadata())
            return {"platform": platform.platform(), "architecture": platform.machine(),
                    "python": platform.python_version(), "cua_driver": EXPECTED_VERSION,
                    "backend_id": BACKEND_ID, "runtime": "in_process_sdk",
                    "native_runtime_verified": metadata is not None,
                    "telemetry_enabled": False, "overlay_enabled": False,
                    "default_delivery": "background", "desktop_locked": False}

    def windows(self) -> list[dict[str, Any]]:
        output = self.bridge.run(self.bridge.driver.list_windows(
            self.sdk.ListWindowsInput(pid=None, on_screen_only=True)))
        records = []
        for window in output.windows[:300]:
            if window.on_current_space is False:
                continue
            try:
                record = self.guard.window(int(window.window_id))
                if record["pid"] == window.pid:
                    records.append(record)
            except ToolError:
                continue
        return sorted(records, key=lambda item: (item["title"].casefold(), item["hwnd"]))

    def current_window(self, hwnd: int) -> dict[str, Any]:
        return self.guard.window(hwnd)

    def validate_window(self, saved: dict[str, Any], *, require_geometry: bool = False) -> dict[str, Any]:
        self.guard.desktop_available()
        current = self.current_window(int(saved["hwnd"]))
        for key in ("pid", "process_created", "exe", "title", "class_name"):
            require(saved.get(key) == current.get(key), "target_changed", "目标窗口或进程身份已变化。", field=key)
        require(current["status"] != "minimized", "target_changed", "目标已最小化，不自动恢复。")
        if require_geometry:
            require(saved.get("bounding_box") == current["bounding_box"] and saved.get("dpi") == current["dpi"],
                    "target_changed", "窗口边界或 DPI 已变化，请重新观察。")
        return current

    def foreground_hwnd(self) -> int:
        return self.guard.foreground_hwnd()

    def require_foreground(self, hwnd: int) -> None:
        require(self.foreground_hwnd() == hwnd, "focus_changed", "目标窗口不是当前前台窗口。")

    def focus(self, hwnd: int) -> dict[str, Any]:
        current = self.current_window(hwnd)
        require(current["status"] != "minimized", "target_changed", "不自动恢复最小化窗口。")
        self.bridge.tool("bring_to_front", {"pid": current["pid"], "window_id": hwnd})
        require(self.foreground_hwnd() == hwnd, "verification_failed", "聚焦后 HWND 与目标不一致。")
        return self.current_window(hwnd)

    def topology(self) -> list[dict[str, Any]]:
        return self.guard.topology()

    def _window_state(self, hwnd: int, *, limit: int = 200, destination: Path | None = None) -> Any:
        window = self.current_window(hwnd)
        require(window["status"] != "minimized", "target_changed", "不观察最小化窗口。")
        return self.bridge.run(self.bridge.driver.get_window_state(self.sdk.GetWindowStateInput(
            pid=window["pid"], window_id=hwnd, session=None, query=None,
            include_accessibility_tree=destination is None, include_screenshot=destination is not None,
            screenshot_out_file=str(destination) if destination else None,
            max_elements=limit, max_depth=25, max_dimension=1920,
        )))

    @staticmethod
    def _node_box(node: Any) -> dict[str, int]:
        frame = node.frame
        return {"left": round(frame.x), "top": round(frame.y),
                "right": round(frame.x + frame.w), "bottom": round(frame.y + frame.h)}

    def snapshot(self, hwnd: int, *, max_elements: int = 200) -> dict[str, Any]:
        limit = max(1, min(max_elements, MAX_TREE_ELEMENTS))
        before = self.current_window(hwnd)
        output = self._window_state(hwnd, limit=limit)
        self.validate_window(before, require_geometry=True)
        require(output.pid == before["pid"] and output.window_id == hwnd,
                "target_changed", "SDK 返回了错误的窗口身份。")
        require(output.degraded is not True, "observation_unavailable", "SDK 窗口树降级，不用于输入。")
        nodes = list(output.elements or [])
        by_index = {node.element_index: node for node in nodes}
        # 上游 label 可能回退为 value；输入控件只展示经独立只读核验的真实 Name。
        labels = {n.element_index: ("" if n.role in {"Edit", "Document"} or
                  (n.value is not None and n.label == n.value) else str(n.label or "")[:512]) for n in nodes}
        identities = {}
        for node in nodes[:limit]:
            if node.role not in {"Edit", "Document"} or node.frame is None:
                continue
            try:
                _control, identity = self.guard.inspect_text_target(
                    hwnd, {"control_type": node.role, "bounding_box": self._node_box(node)})
                identities[node.element_index] = identity
                name = identity["_driver_name"]
                labels[node.element_index] = "" if name == node.value else name[:512]
            except ToolError:
                # 未证明身份的输入节点仍可供观察，但无 RuntimeId 就不能用于 type。
                continue
        elements = []
        for node in nodes[:limit]:
            frame = node.frame
            if frame is None or frame.w <= 0 or frame.h <= 0:
                continue
            parents, index, seen = [], node.parent_index, set()
            while index in by_index and index not in seen and len(parents) < 8:
                seen.add(index)
                parent = by_index[index]
                parents.insert(0, f"{parent.role}:{labels[parent.element_index]}"[:256])
                index = parent.parent_index
            identity = identities.get(node.element_index, {})
            elements.append({
                "name": labels[node.element_index], "control_type": node.role,
                "window_name": before["title"], "parent_path": parents,
                "bounding_box": self._node_box(node),
                "metadata": {"enabled": node.enabled, "actions": list(node.actions or []),
                             "selected": node.selected, "in_web_content": node.in_web_content,
                             "is_password": identity.get("is_password")},
                # token、真实 Name 和 RuntimeId 只供同 worker 重验证，state 不持久化。
                "_driver_token": node.element_token,
                **{key: value for key, value in identity.items() if key.startswith("_driver_")},
            })
        return {"backend_id": BACKEND_ID, "elements": elements,
                "tree_complete": output.elements_complete is True,
                "tree_truncated": output.truncated is True or len(nodes) > limit,
                "element_limit": limit, "topology": self.topology(),
                "foreground_hwnd": self.foreground_hwnd()}

    def capture(self, destination: Path, *, hwnd: int | None = None,
                display: int | None = None) -> dict[str, Any]:
        require(hwnd is not None and display is None, "unsupported_action",
                "此后端仅支持精确窗口截图，不自动升级为显示器或全桌面截图。")
        before = self.current_window(hwnd)
        output = self._window_state(hwnd, destination=destination)
        self.validate_window(before, require_geometry=True)
        bounds = output.window_bounds
        require(output.pid == before["pid"] and output.window_id == hwnd and bounds is not None,
                "target_changed", "截图窗口身份不一致。")
        require(output.screenshot_frame_valid is not False and destination.is_file(),
                "observation_unavailable", "SDK 未产生有效窗口截图。")
        require(destination.stat().st_size <= 10 * 1024 * 1024, "output_too_large", "单张截图超过 10 MiB。")
        with destination.open("rb") as image:
            header = image.read(24)
        require(header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) == 24,
                "observation_unavailable", "SDK 未返回有效 PNG。")
        width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
        require(0 < width <= 1920 and 0 < height <= 1920 and bounds.width > 0 and bounds.height > 0,
                "observation_unavailable", "截图尺寸或窗口边界无效。")
        return {"path": str(destination.resolve()), "size": [width, height],
                "window_hwnd": hwnd, "coordinates_usable": False,
                "warning": "SDK 未返回可验证的实际截图裁剪原点，仅用于视觉验证，不用于像素动作；独立弹窗需单独列窗。"}

    def point_belongs_to_window(self, point: list[int], hwnd: int) -> bool:
        return self.guard.point_belongs(point, hwnd)

    def validate_text_target(self, point: list[int], expected: dict[str, Any], hwnd: int) -> None:
        self.guard.text_control(hwnd, expected)

    def text_value(self, hwnd: int, expected: dict[str, Any]) -> str:
        return self.guard.text_value(hwnd, expected)

    @staticmethod
    def preflight_action(command: str, current: dict[str, Any], element: dict[str, Any] | None,
                         args: dict[str, Any]) -> None:
        delivery = args.get("delivery", "background")
        require(delivery in {"background", "foreground"}, "invalid_argument", "delivery 无效。")
        require(command != "move", "unsupported_action",
                "SDK 的窗口 move_cursor 仅移动 overlay，不冒充真实鼠标操作；本技能不开放全桌面移动。")
        if element is not None:
            require(isinstance(element.get("_driver_token"), str) and bool(element["_driver_token"]),
                    "target_changed", "新快照没有可用的 SDK element token。")
            require(element.get("metadata", {}).get("enabled") is True, "permission_denied", "控件不可用或状态未知。")
        if command == "type":
            require(element is not None, "invalid_argument", "type 必须绑定元素。")
            if args.get("clear"):
                require("set_value" in element.get("metadata", {}).get("actions", []),
                        "unsupported_action", "该控件不提供语义值替换；不使用 Ctrl+A 等隐式组合动作。")
            require(not args.get("press_enter"), "unsupported_action",
                    "输入与提交必须分开观察；输入后重新 snapshot，再显式 shortcut Enter。")
        if delivery == "foreground":
            return
        if command == "type":
            require(bool(args.get("clear")), "background_unavailable",
                    "后台 type 仅允许 --clear 语义替换，不模拟插入位置或全局键盘。")
            return
        require(command == "click", "background_unavailable",
                "此动作尚无审定的窗口级后台路径；不自动转为 foreground。")
        require(element is not None and args.get("button", "left") == "left" and args.get("clicks", 1) == 1,
                "background_unavailable", "后台仅允许元素单次左键点击，拒绝像素、右键和双击注入路径。")
        class_name = current.get("class_name", "")
        allowed = class_name in {"#32770", "Notepad", "WinUIDesktopWin32WindowClass", "ApplicationFrameWindow"}
        allowed = allowed or class_name.startswith(("Chrome_WidgetWin_", "CefBrowser", "WindowsForms10."))
        require(allowed, "background_unavailable",
                "该窗口类未列入后台执行审定范围，拒绝潜在 MSAA/SendInput/触摸注入路径。")

    def execute_action(self, command: str, current: dict[str, Any], args: dict[str, Any], *,
                       point: list[int] | None = None, element: dict[str, Any] | None = None,
                       source: list[int] | None = None, target: list[int] | None = None,
                       text: str | None = None) -> dict[str, Any]:
        hwnd, pid = current["hwnd"], current["pid"]
        mode = args.get("delivery", "background")
        base = {"pid": pid, "window_id": hwnd, "delivery_mode": mode}
        token = element.get("_driver_token") if element else None
        def local(p: list[int]) -> dict[str, int]:
            # 此 worker 没有截图缩放缓存；按固定 SDK 的 bitmap_to_screen 原点逆变换。
            x, y = self.guard.bitmap_origin(hwnd)
            return {"x": p[0] - x, "y": p[1] - y}
        if mode == "foreground":
            self.require_foreground(hwnd)
        if command == "click":
            position = (self.sdk.ClickPosition.ELEMENT(token) if token else
                        self.sdk.ClickPosition.COORDINATES(**local(point)))
            output = self.bridge.run(self.bridge.driver.click(self.sdk.ClickInput(
                target=self.sdk.ActionTarget.WINDOW(pid=pid, window_id=hwnd), position=position,
                delivery_mode=getattr(self.sdk.InputDeliveryMode, mode.upper()), session=None,
                button=getattr(self.sdk.ClickButton, args.get("button", "left").upper()), count=args.get("clicks", 1),
            )))
            verdict = {"effect": enum_name(output.effect), "route": enum_name(output.route),
                       "delivery": enum_name(output.delivery.mode) if output.delivery else None}
            if verdict["effect"] not in {"confirmed", "unverifiable"}:
                raise ToolError("verification_failed", "SDK 未确认完整动作效果；不自动重试。", details=verdict)
            return verdict
        if command == "type":
            self.guard.text_control(hwnd, element)
            if args.get("clear"):
                return self.bridge.tool("set_value", {"pid": pid, "window_id": hwnd,
                                                       "element_token": token, "value": text})
            return self.bridge.tool("type_text", {**base, "element_token": token, "text": text})
        if command == "shortcut":
            keys = args["keys"].split("+")
            keys[-1] = {"enter": "return", "backspace": "backspace"}.get(keys[-1], keys[-1])
            return self.bridge.tool("hotkey" if len(keys) > 1 else "press_key",
                                    {**base, **({"keys": keys} if len(keys) > 1 else {"key": keys[0]})})
        if command == "scroll":
            return self.bridge.tool("scroll", {**base, **local(point), "direction": args.get("direction", "down"),
                                                "amount": args.get("amount", 3)})
        if command == "drag":
            start, end = local(source), local(target)
            return self.bridge.tool("drag", {**base, "from_x": start["x"], "from_y": start["y"],
                                              "to_x": end["x"], "to_y": end["y"],
                                              "duration_ms": round(args.get("duration", 0.5) * 1000)})
        raise ToolError("unsupported_action", "动作未实现。")

    def minimal_observation(self, hwnd: int) -> dict[str, Any]:
        return self.guard.observation(hwnd)


def verify_runtime() -> dict[str, Any]:
    """安装时仅校验包和加载 DLL，不创建 SDK runtime 或访问桌面。"""
    load_sdk()
    return {"ok": True, "status": "compatible", "cua_driver": EXPECTED_VERSION,
            "python": platform.python_version(), "native_runtime_created": False, "telemetry_enabled": False}


if __name__ == "__main__":
    try:
        payload = verify_runtime()
    except ToolError as error:
        payload = {"ok": False, "status": error.code, "message": error.message}
    except Exception as error:
        payload = {"ok": False, "status": "execution_error", "message": type(error).__name__}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if payload.get("ok") else 1)
