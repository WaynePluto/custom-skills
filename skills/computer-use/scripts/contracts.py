#!/usr/bin/env python3
"""computer-use 的公共协议、校验与有界 JSON 输出。"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 24 * 1024
MAX_INPUT_CHARS = 400
MAX_INPUT_BYTES = 4096
MAX_REQUEST_BYTES = 64 * 1024
MAX_TREE_ELEMENTS = 500
SNAPSHOT_TTL_SECONDS = 120
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
REF_RE = re.compile(r"^[wes]-[A-Za-z0-9_-]{6,80}$")

COMMANDS = {
    "doctor", "windows", "focus", "snapshot", "screenshot", "click", "type",
    "shortcut", "scroll", "move", "drag", "wait", "cleanup",
}
ACTION_COMMANDS = {"focus", "click", "type", "shortcut", "scroll", "move", "drag"}
INPUT_COMMANDS = {"click", "type", "shortcut", "scroll", "move", "drag"}
SIDE_EFFECTS = {"none", "dispatched", "uncertain"}

# 快捷键使用精确组合白名单，不接受任意 SendKeys 表达式。
SHORTCUT_WHITELIST = {
    "ctrl+a", "ctrl+f", "ctrl+s", "ctrl+y", "ctrl+z",
    "tab", "shift+tab", "escape", "enter", "space", "backspace", "delete",
    "left", "right", "up", "down", "home", "end", "pageup", "pagedown",
    "ctrl+home", "ctrl+end", "ctrl+left", "ctrl+right",
    "shift+left", "shift+right", "shift+up", "shift+down",
}


@dataclass
class ToolError(Exception):
    """可安全返回给调用者的结构化错误。"""

    code: str
    message: str
    side_effect: str = "none"
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    """条件不满足时抛出结构化错误。"""
    if not condition:
        raise ToolError(code, message, details=details or None)


def validate_session(value: Any, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    require(isinstance(value, str) and SESSION_RE.fullmatch(value) is not None,
            "invalid_argument", "session 只允许 1–40 个英文字母、数字、下划线或连字符。")
    return value


def validate_request_id(value: Any, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    require(isinstance(value, str) and len(value) <= 64,
            "invalid_argument", "动作命令必须提供 UUID 格式的 request-id。")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError("invalid_argument", "动作命令必须提供 UUID 格式的 request-id。") from exc
    require(str(parsed) == value.lower(), "invalid_argument", "request-id 必须使用规范 UUID 文本格式。")
    return value.lower()


def validate_ref(value: Any, kind: str) -> str:
    prefixes = {"window": "w-", "snapshot": "s-", "element": "e-"}
    require(kind in prefixes, "invalid_argument", "内部引用类型无效。")
    require(isinstance(value, str) and REF_RE.fullmatch(value) is not None
            and value.startswith(prefixes[kind]), "invalid_argument", f"{kind} 引用格式无效。")
    return value


def finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    require(not isinstance(value, bool) and isinstance(value, (int, float)),
            "invalid_argument", f"{name} 必须是数字。")
    number = float(value)
    require(math.isfinite(number) and minimum <= number <= maximum,
            "invalid_argument", f"{name} 必须在 {minimum}–{maximum} 之间。")
    return number


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    require(not isinstance(value, bool) and isinstance(value, int),
            "invalid_argument", f"{name} 必须是整数。")
    require(minimum <= value <= maximum, "invalid_argument", f"{name} 必须在 {minimum}–{maximum} 之间。")
    return value


def validate_point(value: Any, name: str = "point") -> list[int]:
    require(isinstance(value, list) and len(value) == 2,
            "invalid_argument", f"{name} 必须是 [x, y] 两个整数。")
    require(all(isinstance(item, int) and not isinstance(item, bool) for item in value),
            "invalid_argument", f"{name} 必须是 [x, y] 两个整数。")
    require(all(-100000 <= item <= 100000 for item in value),
            "invalid_argument", f"{name} 坐标超出允许范围。")
    return value


def normalize_shortcut(value: Any) -> str:
    require(isinstance(value, str), "invalid_argument", "keys 必须是白名单快捷键。")
    normalized = "+".join(part.strip().lower() for part in value.split("+") if part.strip())
    require(normalized in SHORTCUT_WHITELIST, "invalid_argument", "该快捷键未列入安全白名单。",
            allowed=sorted(SHORTCUT_WHITELIST))
    return normalized


def validate_text(text: Any) -> str:
    """校验正文但不把正文放入错误或结果。"""
    require(isinstance(text, str), "invalid_argument", "type 正文必须是 UTF-8 文本。")
    require(0 < len(text) <= MAX_INPUT_CHARS, "invalid_argument",
            f"type 正文必须为 1–{MAX_INPUT_CHARS} 个字符。")
    require(len(text.encode("utf-8")) <= MAX_INPUT_BYTES, "invalid_argument", "type 正文 UTF-8 字节数超限。")
    # 控制字符的跨应用含义不稳定；Enter 由独立参数表达。
    require(not any(ord(ch) < 32 or ord(ch) == 127 for ch in text),
            "invalid_argument", "type 正文不允许控制字符；Enter 必须显式指定。")
    return text


def validate_request(request: Any) -> dict[str, Any]:
    """对 supervisor 与 worker 之间的请求做第二次独立校验。"""
    require(isinstance(request, dict), "invalid_argument", "请求必须是 JSON 对象。")
    require(request.get("schema_version") == SCHEMA_VERSION,
            "invalid_argument", "请求 schema_version 不受支持。")
    command = request.get("command")
    require(command in COMMANDS, "invalid_argument", "未知命令。")
    if command != "doctor":
        request["session"] = validate_session(request.get("session"))
    request["request_id"] = validate_request_id(
        request.get("request_id"), required=command in ACTION_COMMANDS
    )
    timeout = request.get("timeout", 30)
    finite_number(timeout, "timeout", 1, 120)
    deadline = request.get("deadline_monotonic")
    if deadline is not None:
        finite_number(deadline, "deadline_monotonic", 0, 10**12)
    args = request.get("args", {})
    require(isinstance(args, dict), "invalid_argument", "args 必须是 JSON 对象。")
    if command in ACTION_COMMANDS:
        delivery = args.setdefault("delivery", "background")
        require(delivery in {"background", "foreground"}, "invalid_argument", "delivery 只允许 background 或 foreground。")
        if command == "focus":
            require(delivery == "foreground", "background_unavailable", "focus 必须显式指定 --delivery foreground。")
        if command == "click":
            require(args.get("button", "left") in {"left", "right", "middle"}, "invalid_argument", "button 无效。")
            integer(args.get("clicks", 1), "clicks", 1, 2)
        if command == "scroll":
            require(args.get("direction", "down") in {"up", "down", "left", "right"}, "invalid_argument", "direction 无效。")
            integer(args.get("amount", 3), "amount", 1, 20)
        if command == "drag":
            finite_number(args.get("duration", 0.5), "duration", 0, 10)
        if command == "shortcut":
            args["keys"] = normalize_shortcut(args.get("keys"))
    if command == "type":
        validate_text(request.get("input_text"))
    request["args"] = args
    return request


def _redact_sensitive(value: Any, key: str = "") -> Any:
    """纵深防御：任何类似正文的字段都不进入输出。"""
    if key.lower() in {"input_text", "text_body", "stdin", "typed_text", "password", "secret"}:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact_sensitive(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item, key) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item, key) for item in value]
    if isinstance(value, str):
        return value[:4096]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:512]


def result(*, ok: bool, status: str, session: str | None = None,
           request_id: str | None = None, side_effect: str = "none",
           verified: bool = False, data: dict[str, Any] | None = None,
           artifacts: list[dict[str, Any]] | None = None,
           message: str | None = None, details: dict[str, Any] | None = None,
           elapsed_ms: int = 0) -> dict[str, Any]:
    require(side_effect in SIDE_EFFECTS, "execution_error", "内部 side_effect 无效。")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(ok),
        "status": status,
        "request_id": request_id,
        "session": session,
        "side_effect": side_effect,
        "verified": bool(verified),
        "data": _redact_sensitive(data or {}),
        "artifacts": _redact_sensitive(artifacts or []),
        "truncated": False,
        "elapsed_ms": max(0, int(elapsed_ms)),
    }
    if message:
        payload["message"] = str(message)[:1024]
    if details:
        payload["details"] = _redact_sensitive(details)
    return bound_result(payload)


def error_result(error: ToolError, *, session: str | None = None,
                 request_id: str | None = None, elapsed_ms: int = 0) -> dict[str, Any]:
    return result(ok=False, status=error.code, session=session, request_id=request_id,
                  side_effect=error.side_effect, message=error.message,
                  details=error.details, elapsed_ms=elapsed_ms)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _trim_largest_list(value: Any) -> bool:
    """递归缩短最大列表，同时在所属对象旁标记截断。"""
    candidates: list[tuple[int, Any, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in list(node.items()):
                if isinstance(child, list) and child:
                    candidates.append((len(child), node, key))
                visit(child)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                if isinstance(child, list) and child:
                    candidates.append((len(child), node, index))
                visit(child)

    visit(value)
    if not candidates:
        return False
    _length, parent, key = max(candidates, key=lambda item: item[0])
    items = parent[key]
    parent[key] = items[:max(0, len(items) // 2)]
    if isinstance(parent, dict) and isinstance(key, str):
        parent[f"{key}_truncated"] = True
    return True


def bound_result(payload: dict[str, Any], limit: int = MAX_JSON_BYTES) -> dict[str, Any]:
    """按结构递归裁剪结果，优先保留快照和错误语义。"""
    payload = _redact_sensitive(payload)
    if len(_json_bytes(payload)) <= limit:
        return payload
    payload["truncated"] = True
    while len(_json_bytes(payload)) > limit and _trim_largest_list(payload.get("data")):
        pass
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        while len(_json_bytes(payload)) > limit and artifacts:
            artifacts.pop()
    if len(_json_bytes(payload)) > limit:
        payload["data"] = {}
        payload.pop("details", None)
        if "message" in payload:
            payload["message"] = str(payload["message"])[:256]
    if len(_json_bytes(payload)) > limit:
        # 公共字段本身远小于上限；此分支只防御异常调用。
        payload = {
            "schema_version": SCHEMA_VERSION, "ok": False, "status": "output_too_large",
            "request_id": None, "session": None, "side_effect": "none", "verified": False,
            "data": {}, "artifacts": [], "truncated": True, "elapsed_ms": 0,
            "message": "结果超过输出上限。",
        }
    return payload


def dumps_result(payload: dict[str, Any]) -> str:
    return _json_bytes(bound_result(payload)).decode("utf-8")


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
