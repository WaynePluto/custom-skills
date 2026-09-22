#!/usr/bin/env python3
"""computer-use 的受控临时状态、引用代次与请求去重。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from contracts import (
    SCHEMA_VERSION,
    SESSION_RE,
    SNAPSHOT_TTL_SECONDS,
    ToolError,
    validate_ref,
    validate_session,
)

from driver_bridge import BACKEND_ID

STATE_ENV = "COMPUTER_USE_STATE_DIR"
MAX_ARTIFACTS = 20
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
SESSION_MAX_AGE = 24 * 60 * 60


def _is_reparse_entry(path: Path) -> bool:
    """同时识别符号链接与新版 Python 可识别的 Windows junction。"""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def state_root(create: bool = True) -> Path:
    """返回本技能状态根；测试可用环境变量覆盖。"""
    configured = os.environ.get(STATE_ENV)
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ToolError("invalid_state", f"{STATE_ENV} 必须是绝对路径。")
    else:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise ToolError("unsupported_platform", "缺少 LOCALAPPDATA，无法建立受控状态目录。")
        root = Path(local) / "custom-skills" / "computer-use"
    if root.exists() and _is_reparse_entry(root):
        raise ToolError("invalid_state", "状态根目录不能是符号链接或重解析入口。")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=False)


def desktop_generation() -> str:
    """所有 session 共用动作代次；调用者须持有 DesktopMutex。"""
    path = state_root() / "desktop-generation.json"
    if not path.exists():
        return "initial"
    if _is_reparse_entry(path):
        raise ToolError("invalid_state", "桌面动作代次不能是重解析入口。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        token = value["generation"]
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("invalid generation")
        return token
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("invalid_state", "共享桌面动作代次损坏，拒绝输入。") from exc


def _session_dir(session: str, create: bool = True) -> Path:
    validate_session(session)
    root = state_root(create=create)
    sessions = root / "sessions"
    if sessions.exists() and _is_reparse_entry(sessions):
        raise ToolError("invalid_state", "sessions 目录不能是符号链接或重解析入口。")
    if create:
        sessions.mkdir(parents=True, exist_ok=True)
    directory = sessions / session
    if directory.exists() and _is_reparse_entry(directory):
        raise ToolError("invalid_state", "session 目录不能是符号链接或重解析入口。")
    if create:
        directory.mkdir(parents=False, exist_ok=True)
    resolved = directory.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolError("invalid_state", "session 路径越界。") from exc
    return resolved


def _state_path(session: str, create: bool = True) -> Path:
    return _session_dir(session, create=create) / "state.json"


def _new_state(session: str) -> dict[str, Any]:
    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "backend_id": BACKEND_ID,
        "session": session,
        "generation": uuid.uuid4().hex,
        "action_generation": 0,
        "created_at": now,
        "updated_at": now,
        "windows": {},
        "snapshots": {},
        "requests": {},
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """同目录写入并原子替换，避免半份 dispatch 状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_session(session: str, create: bool = True) -> dict[str, Any]:
    path = _state_path(session, create=create)
    if not path.exists():
        if not create:
            raise ToolError("invalid_state", "session 不存在或已 cleanup。")
        value = _new_state(session)
        _atomic_json(path, value)
        return value
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolError("invalid_state", "session 状态损坏，已拒绝继续操作。") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or value.get("backend_id") != BACKEND_ID
            or value.get("session") != session or not isinstance(value.get("generation"), str)):
        raise ToolError("invalid_state", "session 状态版本或身份无效。")
    return value


def save_session(state: dict[str, Any]) -> None:
    session = validate_session(state.get("session"))
    state["updated_at"] = time.time()
    _atomic_json(_state_path(session), state)


def _token_ref(prefix: str, generation: str) -> str:
    return f"{prefix}-{generation[:10]}-{secrets.token_hex(6)}"


def _identity_key(window: dict[str, Any]) -> tuple[Any, ...]:
    return (window.get("hwnd"), window.get("pid"), window.get("process_created"), window.get("exe"))


def register_windows(session: str, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为当前代次窗口建立不复用引用。"""
    state = load_session(session)
    existing = state["windows"]
    by_identity = {_identity_key(item): ref for ref, item in existing.items() if isinstance(item, dict)}
    output = []
    seen: set[str] = set()
    for window in windows:
        ref = by_identity.get(_identity_key(window))
        if not ref:
            ref = _token_ref("w", state["generation"])
        record = dict(window)
        record["window_ref"] = ref
        record["session_generation"] = state["generation"]
        existing[ref] = record
        output.append(record)
        seen.add(ref)
    # 已关闭窗口不再从新清单返回，但保留记录以给出 target_changed。
    save_session(state)
    return output


def get_window(session: str, window_ref: str) -> dict[str, Any]:
    validate_ref(window_ref, "window")
    state = load_session(session, create=False)
    window = state.get("windows", {}).get(window_ref)
    if not isinstance(window, dict) or window.get("session_generation") != state.get("generation"):
        raise ToolError("invalid_state", "window_ref 不属于当前 session 代次。")
    if (not isinstance(window.get("hwnd"), int) or not isinstance(window.get("pid"), int)
            or not isinstance(window.get("process_created"), (int, float))):
        raise ToolError("invalid_state", "窗口进程身份不完整，不能安全用于桌面动作。")
    return dict(window)


def save_snapshot(session: str, window_ref: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """保存快照并分配适配层局部元素引用。"""
    state = load_session(session)
    window = state.get("windows", {}).get(window_ref)
    if not isinstance(window, dict):
        raise ToolError("invalid_state", "window_ref 不属于当前 session。")
    snapshot_ref = _token_ref("s", state["generation"])
    element_map: dict[str, dict[str, Any]] = {}
    public_elements = []
    for item in snapshot.get("elements", []):
        if not isinstance(item, dict):
            continue
        element_ref = _token_ref("e", state["generation"])
        record = {key: value for key, value in item.items() if not key.startswith("_driver_")}
        record["element_ref"] = element_ref
        element_map[element_ref] = record
        public_elements.append(record)
    identity = snapshot.get("window_identity") if isinstance(snapshot.get("window_identity"), dict) else window
    record = dict(snapshot)
    record.update({
        "snapshot_ref": snapshot_ref,
        "session": session,
        "session_generation": state["generation"],
        "action_generation": state["action_generation"],
        "desktop_generation": desktop_generation(),
        "created_at": time.time(),
        "consumed": False,
        "window_ref": window_ref,
        "window_identity": dict(identity),
        "elements": element_map,
    })
    state["snapshots"][snapshot_ref] = record
    save_session(state)
    public = dict(record)
    public["elements"] = public_elements
    return public


def get_snapshot(session: str, snapshot_ref: str, *, for_action: bool = False) -> dict[str, Any]:
    validate_ref(snapshot_ref, "snapshot")
    state = load_session(session, create=False)
    snapshot = state.get("snapshots", {}).get(snapshot_ref)
    if not isinstance(snapshot, dict) or snapshot.get("session_generation") != state.get("generation"):
        raise ToolError("invalid_state", "snapshot 不属于当前 session 代次。")
    if time.time() - float(snapshot.get("created_at", 0)) > SNAPSHOT_TTL_SECONDS:
        raise ToolError("snapshot_expired", "snapshot 已过期，请重新观察。")
    if for_action:
        if snapshot.get("consumed"):
            raise ToolError("snapshot_consumed", "snapshot 已被动作消费，请重新观察。")
        if snapshot.get("action_generation") != state.get("action_generation"):
            raise ToolError("snapshot_expired", "桌面动作代次已变化，请重新观察。")
        if snapshot.get("desktop_generation") != desktop_generation():
            raise ToolError("snapshot_expired", "其它 session 已执行桌面动作，请重新观察。")
    return dict(snapshot)


def get_element(snapshot: dict[str, Any], element_ref: str) -> dict[str, Any]:
    validate_ref(element_ref, "element")
    element = snapshot.get("elements", {}).get(element_ref)
    if not isinstance(element, dict):
        raise ToolError("invalid_state", "element 不属于指定 snapshot。")
    return dict(element)


def request_fingerprint(session: str, request: dict[str, Any]) -> str:
    """使用 session 代次作密钥计算请求指纹，不持久化输入正文或其裸哈希。"""
    current = load_session(session)
    canonical = {
        "command": request.get("command"),
        "args": request.get("args", {}),
        "input_text": request.get("input_text"),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hmac.new(current["generation"].encode("ascii"), encoded, hashlib.sha256).hexdigest()


def get_request_result(session: str, request_id: str,
                       fingerprint: str) -> dict[str, Any] | None:
    """重复 request-id 仅在请求指纹一致时去重；未完成 intent 返回 uncertain。"""
    current = load_session(session)
    record = current.get("requests", {}).get(request_id)
    if not isinstance(record, dict):
        return None
    if record.get("fingerprint") != fingerprint:
        raise ToolError("invalid_state", "request-id 已用于不同请求，拒绝复用。")
    saved = record.get("result")
    if isinstance(saved, dict):
        duplicate = dict(saved)
        duplicate.setdefault("data", {})
        duplicate["data"] = dict(duplicate["data"])
        duplicate["data"]["deduplicated"] = True
        return duplicate
    return {
        "schema_version": SCHEMA_VERSION, "ok": False, "status": "execution_error",
        "request_id": request_id, "session": session, "side_effect": "uncertain",
        "verified": False, "data": {"deduplicated": True}, "artifacts": [],
        "truncated": False, "elapsed_ms": 0,
        "message": "相同 request-id 已留下 dispatch 意图，但结果未知；不会自动重试。",
    }


def commit_dispatch(session: str, request_id: str, command: str,
                    snapshot_ref: str | None = None,
                    fingerprint: str | None = None) -> None:
    """一次原子提交 request 意图、快照消费和桌面动作代次。"""
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ToolError("invalid_argument", "动作请求缺少有效指纹。")
    state = load_session(session)
    if request_id in state.get("requests", {}):
        raise ToolError("invalid_state", "request-id 已存在，拒绝重复下发。")
    if snapshot_ref is not None:
        snapshot = state.get("snapshots", {}).get(snapshot_ref)
        if not isinstance(snapshot, dict):
            raise ToolError("invalid_state", "snapshot 不属于当前 session。")
        if snapshot.get("consumed"):
            raise ToolError("snapshot_consumed", "snapshot 已被动作消费，请重新观察。")
        if snapshot.get("action_generation") != state.get("action_generation"):
            raise ToolError("snapshot_expired", "桌面动作代次已变化，请重新观察。")
        if snapshot.get("desktop_generation") != desktop_generation():
            raise ToolError("snapshot_expired", "其它 session 已执行桌面动作，请重新观察。")
        snapshot["consumed"] = True
    state["action_generation"] = int(state.get("action_generation", 0)) + 1
    state.setdefault("requests", {})[request_id] = {
        "command": command,
        "fingerprint": fingerprint,
        "state": "uncertain",
        "created_at": time.time(),
    }
    save_session(state)
    # 意图和全局代次都成功持久化后才能下发；中途失败可留下 uncertain，但不会执行输入。
    _atomic_json(state_root() / "desktop-generation.json", {"generation": uuid.uuid4().hex})


def complete_request(session: str, request_id: str, payload: dict[str, Any]) -> None:
    state = load_session(session, create=False)
    record = state.get("requests", {}).get(request_id)
    if not isinstance(record, dict):
        raise ToolError("invalid_state", "缺少 dispatch 意图，无法保存动作结果。",
                        side_effect="uncertain")
    record["state"] = "complete"
    record["completed_at"] = time.time()
    record["result"] = payload
    save_session(state)


def artifact_path(session: str, suffix: str = ".png") -> Path:
    """在当前 session 内分配尚未存在的图片路径。"""
    if suffix not in {".png"}:
        raise ToolError("invalid_argument", "不支持的产物格式。")
    directory = _session_dir(session) / "artifacts"
    if directory.exists() and _is_reparse_entry(directory):
        raise ToolError("invalid_state", "产物目录不能是符号链接或重解析入口。")
    directory.mkdir(parents=True, exist_ok=True)
    files = [item for item in directory.iterdir() if item.is_file() and not item.is_symlink()]
    total = sum(item.stat().st_size for item in files)
    if len(files) >= MAX_ARTIFACTS or total >= MAX_ARTIFACT_BYTES:
        raise ToolError("invalid_state", "当前 session 的图片产物配额已用尽，请 cleanup。")
    return directory / f"shot-{int(time.time() * 1000)}-{secrets.token_hex(4)}{suffix}"


def validate_artifact_quota(session: str, artifact: Path) -> None:
    """写入后复核图片总量；超额时只删除本次产物。"""
    directory = _session_dir(session, create=False) / "artifacts"
    try:
        artifact.resolve(strict=True).relative_to(directory.resolve(strict=True))
        files = [item for item in directory.iterdir() if item.is_file() and not item.is_symlink()]
        total = sum(item.stat().st_size for item in files)
    except (OSError, ValueError) as exc:
        artifact.unlink(missing_ok=True)
        raise ToolError("invalid_state", "无法核验截图产物范围。") from exc
    if len(files) > MAX_ARTIFACTS or total > MAX_ARTIFACT_BYTES:
        artifact.unlink(missing_ok=True)
        raise ToolError("invalid_state", "截图产物超过 session 配额，已删除本次图片。")


def _remove_tree_no_follow(directory: Path) -> None:
    """不跟随链接删除技能自有目录。"""
    if not directory.exists():
        return
    for entry in os.scandir(directory):
        path = Path(entry.path)
        if entry.is_symlink() or _is_reparse_entry(path):
            path.unlink(missing_ok=True)
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree_no_follow(path)
            path.rmdir()
        else:
            path.unlink(missing_ok=True)


def cleanup_session(session: str) -> dict[str, Any]:
    validate_session(session)
    directory = _session_dir(session, create=False)
    if not directory.exists():
        return {"removed": False}
    root = state_root(create=False)
    try:
        directory.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ToolError("invalid_state", "cleanup 路径越界。") from exc
    tombstone = directory.with_name(f".{directory.name}.cleanup-{secrets.token_hex(4)}")
    os.replace(directory, tombstone)
    _remove_tree_no_follow(tombstone)
    tombstone.rmdir()
    return {"removed": True}


def purge_expired(now: float | None = None) -> int:
    """只清理身份可验证的过期 session，不跟随任何重解析入口。"""
    now = time.time() if now is None else now
    root = state_root()
    sessions = root / "sessions"
    if not sessions.exists():
        return 0
    if _is_reparse_entry(sessions) or not sessions.is_dir():
        raise ToolError("invalid_state", "sessions 目录不能是符号链接或重解析入口。")
    resolved_sessions = sessions.resolve(strict=True)
    removed = 0
    for directory in sessions.iterdir():
        try:
            if (_is_reparse_entry(directory) or not directory.is_dir()
                    or SESSION_RE.fullmatch(directory.name) is None):
                continue
            resolved = directory.resolve(strict=True)
            resolved.relative_to(resolved_sessions)
            marker = directory / "state.json"
            value = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("session") != directory.name
                    or value.get("schema_version") != SCHEMA_VERSION
                    or now - float(value.get("updated_at", 0)) <= SESSION_MAX_AGE):
                continue
            _remove_tree_no_follow(directory)
            directory.rmdir()
            removed += 1
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
    return removed
