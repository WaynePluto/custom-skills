#!/usr/bin/env python3
"""桌面互斥、元素重定位和坐标前置条件。"""

from __future__ import annotations

import math
import os
import sys
from typing import Any

from contracts import ToolError, require, validate_point

MUTEX_NAME = r"Global\CustomSkillsComputerUseDesktop-v1"


class DesktopMutex:
    """不等待的 Windows named mutex；用于串行化全部桌面观察和输入。"""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self.handle: Any = None
        self._kernel32: Any = None

    def acquire(self) -> None:
        if sys.platform != "win32":
            raise ToolError("unsupported_platform", "computer-use 仅支持 Windows。")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ToolError("execution_error", "无法创建桌面互斥锁。")
        result = kernel32.WaitForSingleObject(handle, 0)
        if result not in (0x00000000, 0x00000080):  # 正常获得或接管被遗弃的互斥量
            kernel32.CloseHandle(handle)
            if result == 0x00000102:
                raise ToolError("desktop_busy", "桌面正被另一个 computer-use 请求占用。")
            raise ToolError("execution_error", "无法获取桌面互斥锁。")
        self.handle = handle
        self._kernel32 = kernel32

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self._kernel32.ReleaseMutex(self.handle)
        finally:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "DesktopMutex":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def box_dict(value: Any) -> dict[str, int]:
    """规范化 left/top/right/bottom 矩形。"""
    require(isinstance(value, dict), "invalid_state", "边界数据无效。")
    try:
        box = {key: int(value[key]) for key in ("left", "top", "right", "bottom")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("invalid_state", "边界数据无效。") from exc
    require(box["right"] > box["left"] and box["bottom"] > box["top"],
            "invalid_state", "边界矩形为空。")
    return box


def point_in_box(point: list[int] | tuple[int, int], box: dict[str, Any]) -> bool:
    normalized = box_dict(box)
    x, y = int(point[0]), int(point[1])
    return normalized["left"] <= x < normalized["right"] and normalized["top"] <= y < normalized["bottom"]


def boxes_approximately_equal(old_value: Any, new_value: Any) -> bool:
    """允许轻微布局抖动，但拒绝明显移动或尺寸变化。"""
    old = box_dict(old_value)
    new = box_dict(new_value)
    old_width = old["right"] - old["left"]
    old_height = old["bottom"] - old["top"]
    new_width = new["right"] - new["left"]
    new_height = new["bottom"] - new["top"]
    old_center = ((old["left"] + old["right"]) / 2, (old["top"] + old["bottom"]) / 2)
    new_center = ((new["left"] + new["right"]) / 2, (new["top"] + new["bottom"]) / 2)
    tolerance_x = max(12.0, old_width * 0.20)
    tolerance_y = max(12.0, old_height * 0.20)
    size_ok = abs(new_width - old_width) <= max(12.0, old_width * 0.30)
    size_ok = size_ok and abs(new_height - old_height) <= max(12.0, old_height * 0.30)
    return (size_ok and abs(new_center[0] - old_center[0]) <= tolerance_x
            and abs(new_center[1] - old_center[1]) <= tolerance_y)


def relocate_element(saved: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """按事实重定位唯一元素，绝不复用上游列表下标或 label。"""
    required = ("name", "control_type", "window_name", "bounding_box")
    require(all(key in saved for key in required), "invalid_state", "快照元素事实不完整。")
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("name") != saved.get("name"):
            continue
        if candidate.get("control_type") != saved.get("control_type"):
            continue
        if candidate.get("window_name") != saved.get("window_name"):
            continue
        saved_path = saved.get("parent_path")
        if saved_path and candidate.get("parent_path") != saved_path:
            continue
        try:
            if boxes_approximately_equal(saved["bounding_box"], candidate.get("bounding_box")):
                matches.append(candidate)
        except ToolError:
            continue
    if not matches:
        raise ToolError("target_changed", "元素已消失或布局变化超过安全阈值。")
    if len(matches) != 1:
        raise ToolError("ambiguous_target", "元素重定位得到多个候选，已拒绝输入。",
                        details={"candidate_count": len(matches)})
    return matches[0]


def element_center(element: dict[str, Any]) -> list[int]:
    box = box_dict(element.get("bounding_box"))
    return [(box["left"] + box["right"]) // 2, (box["top"] + box["bottom"]) // 2]


def image_to_screen(point: Any, screenshot: dict[str, Any]) -> list[int]:
    """把图片像素映射回屏幕坐标；缺少变换数据时 fail closed。"""
    image_point = validate_point(point)
    require(isinstance(screenshot, dict), "invalid_state", "该快照不含可用于坐标动作的截图。")
    origin = screenshot.get("origin")
    size = screenshot.get("size")
    scale = screenshot.get("scale")
    require(isinstance(origin, list) and len(origin) == 2 and
            isinstance(size, list) and len(size) == 2 and
            isinstance(scale, list) and len(scale) == 2,
            "invalid_state", "截图坐标变换数据不完整。")
    require(all(isinstance(item, (int, float)) and not isinstance(item, bool)
                and math.isfinite(float(item)) for item in [*origin, *size, *scale]),
            "invalid_state", "截图坐标变换数据无效。")
    require(scale[0] > 0 and scale[1] > 0 and size[0] > 0 and size[1] > 0,
            "invalid_state", "截图坐标变换数据无效。")
    require(0 <= image_point[0] < int(size[0]) and 0 <= image_point[1] < int(size[1]),
            "invalid_argument", "图片坐标超出截图范围。")
    return [round(float(origin[0]) + image_point[0] / float(scale[0])),
            round(float(origin[1]) + image_point[1] / float(scale[1]))]


def validate_target_point(point: list[int], window: dict[str, Any], backend: Any) -> None:
    """确认坐标在目标窗口内且最上层命中仍属于该窗口。"""
    require(point_in_box(point, window.get("bounding_box")),
            "target_changed", "目标坐标已超出窗口边界。")
    hwnd = int(window.get("hwnd", 0))
    require(hwnd > 0 and backend.point_belongs_to_window(point, hwnd),
            "target_obscured", "目标坐标被其他窗口遮挡或无法确认命中窗口。")


def same_topology(saved: Any, current: Any) -> bool:
    """比较动作相关的显示器身份、边界、DPI 与缩放。"""
    if not isinstance(saved, list) or not isinstance(current, list) or len(saved) != len(current):
        return False
    keys = ("index", "device_name", "bounding_box", "effective_dpi", "scale", "orientation")
    canonical = lambda items: sorted(
        [{key: item.get(key) for key in keys} for item in items if isinstance(item, dict)],
        key=lambda item: (item.get("index", -1), str(item.get("device_name", ""))),
    )
    return canonical(saved) == canonical(current)
