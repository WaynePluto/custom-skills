"""只读 Win32 身份、桌面和 UIA 密码保护；不实现任何输入驱动。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from pathlib import Path
from typing import Any

from contracts import ToolError, require


class WindowsGuard:
    """所有可执行 GUI 动作均留给 cua-driver，本类只提供前后置证据。"""

    def __init__(self) -> None:
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "GetForegroundWindow": (w.HWND, []),
            "IsWindow": (w.BOOL, [w.HWND]),
            "IsWindowVisible": (w.BOOL, [w.HWND]),
            "IsIconic": (w.BOOL, [w.HWND]),
            "GetWindowThreadProcessId": (w.DWORD, [w.HWND, ctypes.POINTER(w.DWORD)]),
            "GetWindowRect": (w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)]),
            "GetWindowTextW": (ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
            "GetClassNameW": (ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
            "GetCursorPos": (w.BOOL, [ctypes.POINTER(w.POINT)]),
            "WindowFromPoint": (w.HWND, [w.POINT]),
            "GetAncestor": (w.HWND, [w.HWND, w.UINT]),
            "OpenInputDesktop": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            "CloseDesktop": (w.BOOL, [w.HANDLE]),
            "GetUserObjectInformationW": (w.BOOL, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]),
            "GetDpiForWindow": (w.UINT, [w.HWND]),
            "SetProcessDpiAwarenessContext": (w.BOOL, [w.HANDLE]),
        }
        for name, (restype, args) in definitions.items():
            fn = getattr(self.user, name)
            fn.restype, fn.argtypes = restype, args
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = w.BOOL
        # 一次性 worker 在读取坐标前使用物理像素，避免 125% DPI 下坐标虚拟化。
        self.user.SetProcessDpiAwarenessContext(w.HANDLE(-4))

    def desktop_available(self) -> None:
        desktop = self.user.OpenInputDesktop(0, False, 1)
        require(bool(desktop), "desktop_unavailable", "无法访问交互桌面，可能处于锁屏或 UAC 安全桌面。")
        try:
            name, needed = ctypes.create_unicode_buffer(256), w.DWORD()
            ok = self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
            require(bool(ok) and name.value.casefold() == "default", "desktop_unavailable", "当前不是默认交互桌面。")
        finally:
            self.user.CloseDesktop(desktop)
        hwnd = self.foreground_hwnd()
        require(hwnd != 0, "desktop_unavailable", "无法确定当前前台窗口。")
        record = self.window(hwnd)
        require(record["process_name"].casefold() not in {"lockapp.exe", "logonui.exe"},
                "desktop_unavailable", "锁屏界面当前位于前台。")

    def window(self, hwnd: int) -> dict[str, Any]:
        require(bool(self.user.IsWindow(hwnd)) and bool(self.user.IsWindowVisible(hwnd)),
                "target_changed", "目标窗口已关闭或隐藏。")
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, w.LPVOID, w.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        cloaked = w.DWORD()
        require(dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value == 0,
                "target_changed", "目标窗口被隐藏或位于其他虚拟桌面。")
        pid, rect = w.DWORD(), w.RECT()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        require(bool(self.user.GetWindowRect(hwnd, ctypes.byref(rect))), "target_changed", "无法读取窗口边界。")
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        require(bool(handle), "permission_denied", "无法只读核验目标进程，不自动提权。")
        try:
            times = [w.FILETIME() for _ in range(4)]
            require(bool(self.kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))),
                    "permission_denied", "无法核验进程创建时间。")
            exe, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            require(bool(self.kernel.QueryFullProcessImageNameW(handle, 0, exe, ctypes.byref(length))),
                    "permission_denied", "无法核验进程路径。")
        finally:
            self.kernel.CloseHandle(handle)
        title, class_name = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(256)
        self.user.GetWindowTextW(hwnd, title, len(title))
        self.user.GetClassNameW(hwnd, class_name, len(class_name))
        return {
            "hwnd": int(hwnd), "pid": pid.value,
            "process_created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "exe": exe.value, "process_name": Path(exe.value).name,
            "title": title.value[:512], "class_name": class_name.value,
            "status": "minimized" if self.user.IsIconic(hwnd) else "normal",
            "dpi": int(self.user.GetDpiForWindow(hwnd)),
            "bounding_box": {k: int(getattr(rect, k)) for k in ("left", "top", "right", "bottom")},
        }

    def foreground_hwnd(self) -> int:
        return int(self.user.GetForegroundWindow() or 0)

    def observation(self, hwnd: int) -> dict[str, Any]:
        cursor = w.POINT()
        require(bool(self.user.GetCursorPos(ctypes.byref(cursor))), "desktop_unavailable", "无法读取鼠标位置。")
        foreground = self.foreground_hwnd()
        return {"foreground_hwnd": foreground, "target_foreground": foreground == hwnd,
                "cursor": [cursor.x, cursor.y]}

    def point_belongs(self, point: list[int], hwnd: int) -> bool:
        hit = self.user.WindowFromPoint(w.POINT(*point))
        return bool(hit) and int(self.user.GetAncestor(hit, 2) or 0) == hwnd

    def bitmap_origin(self, hwnd: int) -> tuple[int, int]:
        """固定 SDK 使用 DWM 左上角内缩一像素；无法核验时拒绝坐标动作。"""
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, w.LPVOID, w.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        rect = w.RECT()
        require(dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0,
                "observation_unavailable", "无法确定 SDK 位图坐标原点。")
        return rect.left + 1, rect.top + 1

    def topology(self) -> list[dict[str, Any]]:
        """读取全部显示器的物理边界，不假定主显示器位于 (0, 0)。"""
        monitors = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
        def collect(handle: Any, _dc: Any, rect: Any, _data: Any) -> bool:
            box = {key: int(getattr(rect.contents, key)) for key in ("left", "top", "right", "bottom")}
            monitors.append({"index": int(handle), "device_name": str(int(handle)), "bounding_box": box})
            return True
        callback = callback_type(collect)
        self.user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
        self.user.EnumDisplayMonitors.restype = w.BOOL
        require(bool(self.user.EnumDisplayMonitors(None, None, callback, 0)) and bool(monitors),
                "desktop_unavailable", "无法枚举显示器拓扑。")
        return sorted(monitors, key=lambda item: item["index"])

    def inspect_text_target(self, hwnd: int, expected: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """按类型和精确边界匹配全部候选，绝不把 SDK fallback label 当作 UIA Name。"""
        require(expected.get("control_type") in {"Edit", "Document"}, "permission_denied",
                "type 只允许已识别的 Edit 或 Document 控件。")
        try:
            import comtypes.client
            comtypes.client.gen_dir = None
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
            root = automation.ElementFromHandle(hwnd)
            kind = 50004 if expected["control_type"] == "Edit" else 50030
            condition = automation.CreatePropertyCondition(30003, kind)
            candidates = root.FindAll(4, condition)
            require(candidates.Length <= 500, "ambiguous_target", "编辑控件过多，无法可靠核验。")
            matches = []
            for index in range(candidates.Length):
                control = candidates.GetElement(index)
                rect = control.CurrentBoundingRectangle
                box = {key: int(getattr(rect, key)) for key in ("left", "top", "right", "bottom")}
                if expected["bounding_box"] == box:
                    matches.append(control)
            require(len(matches) == 1, "ambiguous_target", "类型和边界未唯一确定编辑控件。")
            control = matches[0]
            require(control.CurrentProcessId == self.window(hwnd)["pid"], "target_changed", "编辑控件进程已变化。")
            identity = list(control.GetRuntimeId())
            require(0 < len(identity) <= 64 and all(type(item) is int for item in identity),
                    "permission_denied", "编辑控件缺少可靠 RuntimeId。")
            return control, {"_driver_name": str(control.CurrentName), "_driver_runtime_id": identity,
                             "is_password": bool(control.CurrentIsPassword), "enabled": bool(control.CurrentIsEnabled)}
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError("permission_denied", "无法确认编辑控件的身份或密码属性。",
                            details={"exception": type(exc).__name__}) from exc

    def text_control(self, hwnd: int, expected: dict[str, Any]) -> Any:
        control, identity = self.inspect_text_target(hwnd, expected)
        require(identity["_driver_runtime_id"] == expected.get("_driver_runtime_id") and
                identity["_driver_name"] == expected.get("_driver_name"),
                "target_changed", "编辑控件与本 worker 新快照的只读身份不一致。")
        require(not identity["is_password"], "permission_denied", "拒绝读取或输入密码字段。")
        require(identity["enabled"], "permission_denied", "编辑控件不可用。")
        return control

    def text_value(self, hwnd: int, expected: dict[str, Any]) -> str:
        control = self.text_control(hwnd, expected)
        try:
            return str(control.GetCurrentPropertyValue(30045))
        except Exception as exc:
            raise ToolError("verification_failed", "无法只读取得编辑控件当前值。") from exc
