"""computer-use 公共契约的离线测试；不得加载真实 cua-driver。"""

import importlib
import json
import math
from pathlib import Path
import sys
import unittest
from unittest import mock
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/computer-use/scripts"


def import_without_driver(name):
    """在明确禁止上游包的环境中导入模块。"""
    with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]), \
         mock.patch.object(sys, "dont_write_bytecode", True), \
         mock.patch.dict(sys.modules, {"cua_driver": None}):
        sys.modules.pop(name, None)
        return importlib.import_module(name)


contracts = import_without_driver("contracts")
guard = import_without_driver("guard")
runtime = import_without_driver("runtime")


class ImportBoundaryTests(unittest.TestCase):
    def test_all_cli_modules_import_cross_platform_without_loading_driver(self):
        names = ("contracts", "state", "guard", "backend", "driver_bridge", "win32_guard", "runtime", "computer")
        with mock.patch.object(sys, "platform", "linux"):
            for name in names:
                with self.subTest(module=name):
                    module = import_without_driver(name)
                    self.assertIsNotNone(module)
        loaded = [name for name, value in sys.modules.items()
                  if name.startswith("cua_driver.") and value is not None]
        self.assertEqual(loaded, [])


class ValidationTests(unittest.TestCase):
    def assert_contract_error(self, function, *args, code="invalid_argument", **kwargs):
        with self.assertRaises((contracts.ToolError, guard.ToolError, runtime.ToolError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_session_is_a_bounded_non_path_identifier(self):
        for value in ("task", "Task_01-x", "a" * 40):
            with self.subTest(value=value):
                self.assertEqual(contracts.validate_session(value), value)
        for value in (None, "", ".", "..", "../x", r"..\x", "C:\\tmp", "a/b",
                      "a b", "中文", "a" * 41):
            with self.subTest(value=value):
                self.assert_contract_error(contracts.validate_session, value)

    def test_request_id_requires_uuid_and_is_canonicalized(self):
        value = str(uuid4())
        self.assertEqual(contracts.validate_request_id(value, required=True), value)
        self.assertEqual(contracts.validate_request_id(value.upper(), required=True), value)
        self.assertIsNone(contracts.validate_request_id(None, required=False))
        for invalid in (None, "", "request-1", "{" + value + "}"):
            with self.subTest(value=invalid):
                self.assert_contract_error(
                    contracts.validate_request_id, invalid, required=True)

    def test_request_validation_canonicalizes_uuid_for_deduplication(self):
        request_id = str(uuid4())
        request = {
            "schema_version": 1,
            "command": "focus",
            "session": "task",
            "request_id": request_id.upper(),
            "timeout": 30,
            "args": {"window": "w-example", "delivery": "foreground"},
        }
        contracts.validate_request(request)
        self.assertEqual(request["request_id"], request_id)

    def test_type_budget_rejects_before_dispatch_when_deadline_is_too_short(self):
        request = {
            "input_text": "x" * 100,
            "args": {"clear": True},
            "deadline_monotonic": 101.0,
        }
        with mock.patch.object(runtime.time, "monotonic", return_value=100.0):
            self.assert_contract_error(runtime._require_type_budget, request, code="timeout")

    def test_drag_budget_rejects_before_dispatch_when_deadline_is_too_short(self):
        request = {"deadline_monotonic": 105.0}
        with mock.patch.object(runtime.time, "monotonic", return_value=100.0):
            self.assert_contract_error(
                runtime._require_action_budget, request, 12.0, "drag", code="timeout"
            )

    def test_screen_points_allow_negative_origins_but_reject_non_integral_values(self):
        self.assertEqual(contracts.validate_point([-1920, 25]), [-1920, 25])
        for point in ([math.nan, 0], [math.inf, 0], [0, -math.inf], [1.5, 2], [True, 2],
                      [0], [0, 1, 2], "0,1", [-100001, 0]):
            with self.subTest(point=point):
                self.assert_contract_error(contracts.validate_point, point)

    def test_image_coordinates_are_bounded_and_transform_negative_origins(self):
        screenshot = {"origin": [-1920, 50], "size": [10, 5], "scale": [0.5, 2.0]}
        self.assertEqual(guard.image_to_screen([9, 4], screenshot), [-1902, 52])
        for point in ([-1, 0], [0, -1], [10, 0], [0, 5]):
            with self.subTest(point=point):
                self.assert_contract_error(guard.image_to_screen, point, screenshot)

    def test_element_relocation_uses_parent_path_when_available(self):
        saved = {
            "name": "确定", "control_type": "Button", "window_name": "Dialog",
            "parent_path": ["Pane:Primary"],
            "bounding_box": {"left": 10, "top": 10, "right": 30, "bottom": 30},
        }
        wrong_parent = dict(saved, parent_path=["Pane:Secondary"])
        self.assert_contract_error(guard.relocate_element, saved, [wrong_parent], code="target_changed")
        self.assertEqual(guard.relocate_element(saved, [saved]), saved)

    def test_shortcuts_are_parsed_from_a_small_key_whitelist(self):
        self.assertEqual(contracts.normalize_shortcut(" CTRL + S "), "ctrl+s")
        self.assertEqual(contracts.normalize_shortcut("shift+tab"), "shift+tab")
        for value in ("", "s", "ctrl", "ctrl+ctrl+s", "ctrl+unknown",
                      "ctrl+s;enter", "{CTRL}S", "win+r", "ctrl+alt+delete", "alt+f4"):
            with self.subTest(value=value):
                self.assert_contract_error(contracts.normalize_shortcut, value)

    def test_text_is_literal_unicode_bounded_and_rejects_controls(self):
        value = "中文 + {ENTER} 不应作为 SendKeys 指令"
        self.assertEqual(contracts.validate_text(value), value)
        self.assertEqual(contracts.validate_text("x" * 400), "x" * 400)
        for value in ("", "x" * 401, "nul\0byte", "line\rbreak", "line\nbreak"):
            with self.subTest(length=len(value)):
                self.assert_contract_error(contracts.validate_text, value)


class ResultEncodingTests(unittest.TestCase):
    def decode(self, encoded):
        self.assertIsInstance(encoded, str)
        self.assertEqual(len(encoded.splitlines()), 1)
        return encoded, json.loads(encoded)

    def test_result_is_one_complete_json_object_and_preserves_protocol_fields(self):
        result = contracts.result(
            ok=True, status="completed", request_id=str(uuid4()), session="task",
            side_effect="none", data={"windows": [{"title": "记事本"}]}, elapsed_ms=3)
        encoded, decoded = self.decode(contracts.dumps_result(result))
        self.assertEqual(decoded, result)
        self.assertLessEqual(len(encoded.encode("utf-8")), contracts.MAX_JSON_BYTES)

    def test_oversized_data_is_trimmed_without_cutting_json_or_error_fields(self):
        result = {
            "schema_version": 1, "ok": False, "status": "execution_error",
            "message": "worker failed", "side_effect": "none", "verified": False,
            "data": {"nodes": [{"name": "界" * 200} for _ in range(100)]},
            "artifacts": [], "truncated": False, "elapsed_ms": 5,
        }
        bounded = contracts.bound_result(result, limit=2048)
        encoded, decoded = self.decode(contracts.dumps_result(bounded))
        self.assertLessEqual(len(json.dumps(
            decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 2048)
        self.assertEqual(decoded["status"], "execution_error")
        self.assertEqual(decoded["message"], "worker failed")
        self.assertTrue(decoded["truncated"])
        self.assertIn("nodes", decoded["data"])
        self.assertTrue(decoded["data"]["nodes_truncated"])
        self.assertGreater(len(decoded["data"]["nodes"]), 0)

    def test_text_body_is_redacted_from_result(self):
        secret = "SECRET-正文-123"
        result = contracts.result(
            ok=True, status="dispatched", side_effect="dispatched",
            data={"input_text": secret, "text_length": len(secret)})
        encoded, decoded = self.decode(contracts.dumps_result(result))
        self.assertEqual(decoded["data"]["text_length"], len(secret))
        self.assertEqual(decoded["data"]["input_text"], "[redacted]")
        self.assertNotIn(secret, encoded)


if __name__ == "__main__":
    unittest.main()
