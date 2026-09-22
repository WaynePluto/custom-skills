"""锁定 cua-driver 同进程 SDK；不启动二进制、MCP 或 daemon。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from contracts import ToolError, require

EXPECTED_VERSION = "0.28.2"
BACKEND_ID = f"cua-driver/{EXPECTED_VERSION}/sdk-v1"
MANIFEST = Path(__file__).with_name("compatibility.json")


def verify_package(record: dict[str, Any]) -> None:
    """校验绑定源码、DLL 和附带二进制；不把 wheel 哈希冒充源码构建证明。"""
    name = record["distribution"]
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ToolError("runtime_missing", f"专用 runtime 未安装 {name}。") from exc
    require(distribution.version == record["version"], "incompatible_version", f"{name} 版本不在审定范围。")
    root = Path(distribution.locate_file(record["module"]))
    files = sorted((p for p in root.rglob("*") if p.suffix in {".py", ".dll", ".exe"}
                    and "gen" not in p.relative_to(root).parts), key=lambda p: p.relative_to(root).as_posix())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    require(len(files) == record["file_count"] and digest.hexdigest() == record["code_tree_sha256"],
            "incompatible_version", f"{name} 的源码或原生文件与锁定 artifact 不一致。")


def load_sdk() -> Any:
    require(sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64", "x64"},
            "unsupported_platform", "computer-use 仅支持 Windows x64。")
    require(sys.version_info[:2] == (3, 13) and platform.python_implementation() == "CPython",
            "incompatible_version", "专用 runtime 必须使用 CPython 3.13。")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for package in manifest["packages"]:
        verify_package(package)
    os.environ["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    os.environ["CUA_TELEMETRY_ENABLED"] = "0"
    # 不继承宿主为其它客户端设置的授权升级、远端连接或录制配置。
    for name in list(os.environ):
        if name.startswith("CUA_") and name not in {"CUA_DRIVER_RS_TELEMETRY_ENABLED", "CUA_TELEMETRY_ENABLED"}:
            os.environ.pop(name)
    sdk = importlib.import_module("cua_driver")
    for name, fields in manifest["interfaces"].items():
        cls = getattr(sdk, name, None)
        require(cls is not None, "incompatible_version", "SDK 接口不存在。", interface=name)
        actual = set(dir(cls)) if name == "CuaDriver" else set(inspect.signature(cls).parameters)
        require(set(fields) <= actual, "incompatible_version", "SDK 接口指纹不匹配。", interface=name)
    return sdk


def enum_name(value: Any) -> str | None:
    return str(value.name).lower() if getattr(value, "name", None) else None


class DriverSession:
    """一个 worker 一个事件循环与 native runtime，正常结束时必须 shutdown。"""

    def __init__(self, sdk: Any) -> None:
        self.sdk = sdk
        self.loop = asyncio.new_event_loop()
        self.driver: Any = None
        try:
            self.driver = sdk.CuaDriver.create()
        except Exception as exc:
            self.loop.close()
            raise ToolError("desktop_unavailable", "无法创建 cua-driver 同进程 runtime。",
                            details={"exception": type(exc).__name__}) from exc

    def run(self, awaitable: Any) -> Any:
        try:
            return self.loop.run_until_complete(awaitable)
        except self.sdk.DriverError as exc:
            code = getattr(exc, "error_code", None)
            raise ToolError("driver_refused", "cua-driver 拒绝请求；不自动重试或升级执行方式。",
                            details={"driver_code": str(code)[:100] if code else None,
                                     "exception": type(exc).__name__}) from exc

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """仅允许经过适配的内部动作；原始正文和错误消息不向调用者透传。"""
        require(name in {"bring_to_front", "type_text", "set_value", "press_key", "hotkey", "scroll", "drag"},
                "permission_denied", "内部 SDK 工具不在白名单。")
        output = self.run(self.driver.call_tool(name, json.dumps(args, ensure_ascii=False)))
        if output.is_error:
            raise ToolError("driver_refused", "cua-driver 拒绝动作；动作可能已经部分发生。",
                            details={"driver_code": str(output.error_code)[:100] if output.error_code else None})
        structured = json.loads(output.structured_json) if output.structured_json else {}
        return {key: structured[key] for key in ("effect", "route", "delivery") if key in structured}

    def close(self) -> None:
        try:
            if self.driver is not None:
                self.run(self.driver.shutdown())
        finally:
            self.driver = None
            self.loop.close()
