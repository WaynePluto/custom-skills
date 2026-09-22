"""cua-driver 安全边界离线测试；禁止创建真实 SDK 或操作桌面。"""

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/computer-use/scripts"
sys.path.insert(0, str(SCRIPTS))
import backend
import driver_bridge
import win32_guard


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.window = {"pid": 42, "hwnd": 123, "title": "Dialog", "class_name": "Chrome_WidgetWin_1",
                       "bounding_box": {"left": 2560, "top": 0, "right": 3840, "bottom": 1000}}
        self.element = {"name": "允许", "control_type": "Button", "_driver_token": "fresh",
                        "metadata": {"enabled": True, "actions": ["invoke"]}}

    def refuse(self, action, args=None, element=True, class_name=None):
        window = dict(self.window, class_name=class_name or self.window["class_name"])
        with self.assertRaises(backend.ToolError) as caught:
            backend.CuaBackend.preflight_action(action, window, self.element if element else None, args or {})
        return caught.exception.code

    def test_background_chrome_single_element_click_is_allowed(self):
        backend.CuaBackend.preflight_action("click", self.window, self.element, {})

    def test_background_rejects_known_injection_paths_before_sdk(self):
        for name in ("SALFRAME", "SALSUBFRAME", "HwndWrapper[test]", "gdkWindowToplevel", "Unknown"):
            with self.subTest(name=name):
                self.assertEqual(self.refuse("click", class_name=name), "background_unavailable")
        for args in ({"button": "right"}, {"button": "middle"}, {"clicks": 2}):
            self.assertEqual(self.refuse("click", args), "background_unavailable")
        self.assertEqual(self.refuse("click", element=False), "background_unavailable")

    def test_background_unimplemented_gestures_are_not_silently_global(self):
        for command in ("scroll", "drag", "shortcut"):
            self.assertEqual(self.refuse(command, element=False), "background_unavailable")
        self.assertEqual(self.refuse("move", {"delivery": "foreground"}), "unsupported_action")

    def test_background_type_is_single_semantic_write_not_insert_or_submit(self):
        self.element["metadata"]["actions"] = ["set_value"]
        backend.CuaBackend.preflight_action("type", self.window, self.element, {"clear": True})
        self.assertEqual(self.refuse("type"), "background_unavailable")
        self.assertEqual(self.refuse("type", {"clear": True, "press_enter": True}), "unsupported_action")

    def test_unknown_enabled_or_missing_native_token_refuses(self):
        self.element["metadata"]["enabled"] = None
        self.assertEqual(self.refuse("click"), "permission_denied")
        self.element["_driver_token"] = None
        self.assertEqual(self.refuse("click"), "target_changed")

    def test_sdk_click_always_binds_exact_pid_hwnd_fresh_token_and_mode(self):
        instance = backend.CuaBackend.__new__(backend.CuaBackend)
        instance.sdk = NS(
            ClickInput=lambda **kw: kw, ActionTarget=NS(WINDOW=lambda **kw: kw),
            ClickPosition=NS(ELEMENT=lambda token: {"element_token": token}),
            InputDeliveryMode=NS(BACKGROUND="background"), ClickButton=NS(LEFT="left"),
        )
        verdict = NS(effect=NS(name="UNVERIFIABLE"), route=NS(name="WINDOWS_POST_MESSAGE"),
                     delivery=NS(mode=NS(name="BACKGROUND")))
        instance.bridge = mock.Mock()
        instance.bridge.run.return_value = verdict
        result = instance.execute_action("click", self.window, {}, element=self.element)
        args = instance.bridge.driver.click.call_args.args[0]
        self.assertEqual(args["target"], {"pid": 42, "window_id": 123})
        self.assertEqual(args["position"], {"element_token": "fresh"})
        self.assertEqual(args["delivery_mode"], "background")
        instance.bridge.driver.click.assert_called_once()
        self.assertEqual(result["effect"], "unverifiable")

    def test_snapshot_is_background_read_without_screenshot_or_value_leak(self):
        instance = backend.CuaBackend.__new__(backend.CuaBackend)
        node = NS(element_index=1, parent_index=None, role="Edit", label="输入",
                  value="SHOULD_NOT_LEAK", actions=["set_value"], enabled=True,
                  selected=None, in_web_content=False, element_token="native-token",
                  frame=NS(x=2600, y=100, w=80, h=40))
        output = NS(pid=42, window_id=123, elements=[node], degraded=False,
                    truncated=False, elements_complete=None)
        instance.current_window = mock.Mock(return_value=self.window)
        instance.validate_window = mock.Mock()
        instance._window_state = mock.Mock(return_value=output)
        instance.guard = NS(inspect_text_target=mock.Mock(side_effect=backend.ToolError("permission_denied", "unknown")))
        node.label = node.value
        instance.topology = mock.Mock(return_value=[])
        instance.foreground_hwnd = mock.Mock(return_value=999)
        result = instance.snapshot(123)
        self.assertFalse(result["tree_complete"])
        self.assertEqual(result["foreground_hwnd"], 999)
        self.assertNotIn("SHOULD_NOT_LEAK", json.dumps(result))
        self.assertEqual(result["elements"][0]["bounding_box"]["left"], 2600)
        self.assertEqual(result["elements"][0]["_driver_token"], "native-token")

    def test_window_snapshot_calls_typed_sdk_with_no_hidden_screenshot(self):
        instance = backend.CuaBackend.__new__(backend.CuaBackend)
        instance.current_window = mock.Mock(return_value=dict(self.window, status="normal"))
        instance.sdk = NS(GetWindowStateInput=lambda **kw: kw)
        instance.bridge = mock.Mock()
        instance._window_state(123, limit=50)
        args = instance.bridge.driver.get_window_state.call_args.args[0]
        self.assertFalse(args["include_screenshot"])
        self.assertTrue(args["include_accessibility_tree"])
        self.assertEqual(args["window_id"], 123)
        self.assertEqual(args["max_elements"], 50)

    def test_driver_error_is_not_retried_or_returned_with_sensitive_message(self):
        class DriverError(Exception):
            error_code = "background_unavailable"
        async def fail():
            raise DriverError("SECRET typed body")
        bridge = driver_bridge.DriverSession.__new__(driver_bridge.DriverSession)
        bridge.sdk = NS(DriverError=DriverError)
        bridge.loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(driver_bridge.ToolError) as caught:
                bridge.run(fail())
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(caught.exception.details["driver_code"], "background_unavailable")
        finally:
            bridge.loop.close()

    def test_close_always_shuts_down_and_closes_event_loop(self):
        async def stop():
            return None
        driver = mock.Mock(shutdown=mock.Mock(side_effect=stop))
        sdk = NS(CuaDriver=NS(create=lambda: driver), DriverError=RuntimeError)
        bridge = driver_bridge.DriverSession(sdk)
        bridge.close()
        driver.shutdown.assert_called_once()
        self.assertTrue(bridge.loop.is_closed())
        self.assertIsNone(bridge.driver)


class PackageTests(unittest.TestCase):
    def test_binary_modification_and_added_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "module").mkdir()
            binary = root / "module/native.dll"
            binary.write_bytes(b"native code")
            digest = hashlib.sha256(b"native.dll\0native code\0").hexdigest()
            record = {"distribution": "test", "module": "module", "version": "1",
                      "file_count": 1, "code_tree_sha256": digest}
            dist = NS(version="1", locate_file=lambda name: root / name)
            with mock.patch.object(driver_bridge.importlib.metadata, "distribution", return_value=dist):
                driver_bridge.verify_package(record)
                binary.write_bytes(b"tampered")
                with self.assertRaises(driver_bridge.ToolError):
                    driver_bridge.verify_package(record)
                binary.write_bytes(b"native code")
                (root / "module/extra.py").write_bytes(b"x")
                with self.assertRaises(driver_bridge.ToolError):
                    driver_bridge.verify_package(record)


class PasswordGuardTests(unittest.TestCase):
    def test_password_unknown_or_duplicate_control_is_refused(self):
        guard = win32_guard.WindowsGuard.__new__(win32_guard.WindowsGuard)
        guard.window = mock.Mock(return_value={"pid": 42})
        expected = {"name": "Text", "control_type": "Edit", "_driver_name": "", "_driver_runtime_id": [42, 3],
                    "bounding_box": {"left": 10, "top": 20, "right": 100, "bottom": 40}}
        def control(password):
            return NS(CurrentBoundingRectangle=NS(**expected["bounding_box"]), CurrentIsPassword=password,
                      CurrentName="", GetRuntimeId=lambda: [42, 3], CurrentIsEnabled=True, CurrentProcessId=42)
        automation = mock.Mock()
        module = NS(CUIAutomation="class", IUIAutomation="interface")
        client = NS(GetModule=lambda _: module, CreateObject=lambda *_a, **_kw: automation)
        package = NS(client=client)
        for nodes in ([control(True)], [], [control(False), control(False)]):
            automation.ElementFromHandle.return_value.FindAll.return_value = NS(
                Length=len(nodes), GetElement=lambda i: nodes[i])
            with mock.patch.dict(sys.modules, {"comtypes": package, "comtypes.client": client}):
                with self.assertRaises(win32_guard.ToolError):
                    guard.text_control(123, expected)
        nodes = [control(False)]
        automation.ElementFromHandle.return_value.FindAll.return_value = NS(Length=1, GetElement=lambda i: nodes[i])
        with mock.patch.dict(sys.modules, {"comtypes": package, "comtypes.client": client}):
            self.assertIs(guard.text_control(123, expected), nodes[0])
            expected["_driver_runtime_id"] = [999]
            with self.assertRaises(win32_guard.ToolError):
                guard.text_control(123, expected)
            automation.CreateAndCondition.assert_not_called()

    def test_fallback_label_cannot_select_another_non_password_control(self):
        guard = win32_guard.WindowsGuard.__new__(win32_guard.WindowsGuard)
        guard.inspect_text_target = mock.Mock(return_value=(None, {
            "_driver_name": "", "_driver_runtime_id": [1, 2], "is_password": True, "enabled": True}))
        with self.assertRaises(win32_guard.ToolError):
            guard.text_control(123, {"name": "ordinary label", "_driver_name": "ordinary label",
                                     "_driver_runtime_id": [1, 3]})

    def test_locked_or_unknown_desktop_is_rejected_without_sdk(self):
        guard = win32_guard.WindowsGuard.__new__(win32_guard.WindowsGuard)
        guard.user = mock.Mock()
        guard.user.OpenInputDesktop.return_value = 0
        with self.assertRaises(win32_guard.ToolError) as caught:
            guard.desktop_available()
        self.assertEqual(caught.exception.code, "desktop_unavailable")


if __name__ == "__main__":
    unittest.main()
