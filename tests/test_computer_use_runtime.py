"""真实状态存储 + fake SDK 后端的端到端路由测试，不接触桌面。"""

import copy
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/computer-use/scripts"))
import runtime


class FakeBackend:
    def __init__(self):
        self.window = {"hwnd": 10, "pid": 20, "process_created": 30, "exe": "test.exe",
                       "title": "test", "class_name": "Chrome_WidgetWin_1", "status": "normal", "dpi": 120,
                       "bounding_box": {"left": 2560, "top": 0, "right": 4000, "bottom": 1000}}
        self.element = {"name": "允许", "control_type": "Button", "window_name": "test", "parent_path": [],
                        "bounding_box": {"left": 2600, "top": 100, "right": 2700, "bottom": 200},
                        "metadata": {"enabled": True, "actions": ["invoke"]}, "_driver_token": "worker-token"}
        self.calls = []
        self.error = None
        self.changed = False
        self.cursor_moved = False
        self.foreground = 999
        self.closed = 0
        self.close_error = None
        self.focus_checks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed += 1
        if self.close_error:
            raise self.close_error

    def windows(self):
        return [self.window]

    def validate_window(self, saved, **_kw):
        if self.changed:
            raise runtime.ToolError("target_changed", "changed")
        return self.window

    def snapshot(self, hwnd, **_kw):
        return {"backend_id": runtime.BACKEND_ID, "elements": [copy.deepcopy(self.element)],
                "tree_complete": False, "element_limit": 200, "topology": self.topology(),
                "foreground_hwnd": self.foreground}

    def topology(self):
        return [{"index": 1, "bounding_box": self.window["bounding_box"], "device_name": "monitor"}]

    def foreground_hwnd(self):
        return self.foreground

    def require_foreground(self, hwnd):
        self.focus_checks += 1
        if self.foreground != hwnd:
            raise runtime.ToolError("focus_changed", "not foreground")

    def point_belongs_to_window(self, *_args):
        return False

    def preflight_action(self, command, current, element, args):
        runtime.CuaBackendActual.preflight_action(command, current, element, args)

    def execute_action(self, command, current, args, **kw):
        self.calls.append((command, args, kw))
        if self.error:
            raise self.error
        return {"effect": "unverifiable", "delivery": "background"}

    def minimal_observation(self, hwnd):
        return {"foreground_hwnd": self.foreground, "target_foreground": self.foreground == hwnd,
                "cursor": [9, 9] if self.cursor_moved and self.calls else [1, 1]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = mock.patch.dict(runtime.os.environ, {runtime.state.STATE_ENV: self.temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend = FakeBackend()
        actual = runtime.CuaBackend
        self.actual_patch = mock.patch.object(runtime, "CuaBackendActual", actual, create=True)
        self.actual_patch.start()
        self.addCleanup(self.actual_patch.stop)
        self.factory = mock.patch.object(runtime, "CuaBackend", return_value=self.backend)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.mutex = mock.patch.object(runtime, "DesktopMutex", side_effect=lambda: nullcontext())
        self.mutex.start()
        self.addCleanup(self.mutex.stop)
        windows = self.call("windows")["data"]["windows"]
        self.window_ref = windows[0]["window_ref"]
        self.snapshot = self.call("snapshot", {"window": self.window_ref})["data"]["snapshot"]

    def request(self, command, args=None, request_id=None):
        return {"schema_version": 1, "command": command, "session": "case", "timeout": 60,
                "request_id": request_id or (str(uuid4()) if command in runtime.ACTION_COMMANDS else None),
                "deadline_monotonic": time.monotonic() + 60, "args": args or {}}

    def call(self, command, args=None):
        return runtime.handle(self.request(command, args), time.monotonic())

    def click_args(self, **kw):
        return {"snapshot": self.snapshot["snapshot_ref"], "element": self.snapshot["elements"][0]["element_ref"], **kw}

    def test_background_observation_and_click_do_not_require_foreground_or_unoccluded_point(self):
        payload = self.call("click", self.click_args())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["side_effect"], "dispatched")
        self.assertEqual(self.backend.focus_checks, 0)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.calls[0][2]["element"]["_driver_token"], "worker-token")
        self.assertEqual(self.backend.closed, 3)

    def test_upstream_token_not_public_or_persisted_and_action_uses_fresh_token(self):
        self.assertNotIn("_driver_token", self.snapshot["elements"][0])
        saved = runtime.state.get_snapshot("case", self.snapshot["snapshot_ref"])
        self.assertNotIn("worker-token", str(saved))
        self.backend.element["_driver_token"] = "new-worker-token"
        self.call("click", self.click_args())
        self.assertEqual(self.backend.calls[0][2]["element"]["_driver_token"], "new-worker-token")

    def test_background_off_window_element_is_refused_without_auto_scroll(self):
        self.backend.element["bounding_box"] = {"left": -32000, "top": -32000, "right": -31900, "bottom": -31900}
        self.snapshot = self.call("snapshot", {"window": self.window_ref})["data"]["snapshot"]
        result = self.call("click", self.click_args())
        self.assertEqual(result["status"], "target_changed")
        self.assertFalse(self.backend.calls)

    def test_background_refusal_before_dispatch_keeps_snapshot_unused(self):
        payload = self.call("click", self.click_args(button="right"))
        self.assertEqual(payload["status"], "background_unavailable")
        self.assertEqual(payload["side_effect"], "none")
        self.assertFalse(self.backend.calls)
        self.assertFalse(runtime.state.get_snapshot("case", self.snapshot["snapshot_ref"])["consumed"])

    def test_driver_error_after_intent_is_uncertain_and_same_id_never_replays(self):
        self.backend.error = runtime.ToolError("driver_refused", "could have acted")
        request = self.request("click", self.click_args())
        payload = runtime.handle(request, time.monotonic())
        self.assertEqual(payload["side_effect"], "uncertain")
        repeated = runtime.handle(request, time.monotonic())
        self.assertTrue(repeated["data"]["deduplicated"])
        self.assertEqual(len(self.backend.calls), 1)

    def test_changed_cursor_is_uncertain_not_restored_and_not_replayed(self):
        self.backend.cursor_moved = True
        request = self.request("click", self.click_args())
        payload = runtime.handle(request, time.monotonic())
        self.assertEqual(payload["status"], "interference_detected")
        self.assertEqual(payload["side_effect"], "uncertain")
        runtime.handle(request, time.monotonic())
        self.assertEqual(len(self.backend.calls), 1)

    def test_second_action_with_consumed_snapshot_is_rejected(self):
        self.call("click", self.click_args())
        payload = self.call("click", self.click_args())
        self.assertEqual(payload["status"], "snapshot_consumed")
        self.assertEqual(len(self.backend.calls), 1)

    def test_window_change_or_ambiguous_element_cannot_dispatch(self):
        self.backend.changed = True
        self.assertEqual(self.call("click", self.click_args())["status"], "target_changed")
        self.backend.changed = False
        original = self.backend.snapshot
        def duplicate(*args, **kw):
            snapshot = original(*args, **kw)
            snapshot["elements"] *= 2
            return snapshot
        self.backend.snapshot = duplicate
        self.assertEqual(self.call("click", self.click_args())["status"], "ambiguous_target")
        self.assertFalse(self.backend.calls)

    def test_explicit_foreground_action_still_requires_target_foreground(self):
        payload = self.call("click", self.click_args(delivery="foreground"))
        self.assertEqual(payload["status"], "focus_changed")
        self.assertFalse(self.backend.calls)

    def test_wait_can_find_background_element_without_focus(self):
        result = self.call("wait", {"window": self.window_ref, "condition": "appears", "name": "允许", "wait_seconds": 0.1})
        self.assertEqual(result["status"], "condition_met")
        self.assertFalse(self.backend.calls)

    def test_incomplete_tree_never_proves_element_disappeared(self):
        request = self.request("wait", {"window": self.window_ref, "condition": "disappears", "name": "missing", "wait_seconds": 0.1})
        with self.assertRaises(runtime.ToolError) as caught:
            runtime.handle(request, time.monotonic())
        self.assertEqual(caught.exception.code, "verification_failed")

    def test_old_backend_session_requires_cleanup_not_silent_reuse(self):
        current = runtime.state.load_session("case")
        current.pop("backend_id")
        runtime.state.save_session(current)
        with self.assertRaises(runtime.state.ToolError):
            runtime.state.load_session("case")
        self.assertTrue(self.call("cleanup")["ok"])
        self.assertTrue(self.call("windows")["ok"])

    def test_result_persistence_failure_after_dispatch_is_uncertain(self):
        request = self.request("click", self.click_args())
        with mock.patch.object(runtime.state, "complete_request", side_effect=OSError("disk full")):
            payload = runtime.handle(request, time.monotonic())
        self.assertEqual(payload["side_effect"], "uncertain")
        self.assertEqual(len(self.backend.calls), 1)
        repeated = runtime.handle(request, time.monotonic())
        self.assertEqual(repeated["side_effect"], "uncertain")
        self.assertEqual(len(self.backend.calls), 1)

    def test_shutdown_failure_cannot_erase_dispatch_even_when_state_write_fails(self):
        self.backend.close_error = OSError("shutdown failed")
        with mock.patch.object(runtime.state, "complete_request", side_effect=OSError("disk full")):
            payload = self.call("click", self.click_args())
        self.assertEqual(payload["side_effect"], "uncertain")
        self.assertEqual(len(self.backend.calls), 1)

    def test_other_session_action_expires_this_sessions_snapshot(self):
        runtime.state.load_session("other")
        fingerprint = runtime.state.request_fingerprint("other", {"command": "focus", "args": {}})
        runtime.state.commit_dispatch("other", str(uuid4()), "focus", fingerprint=fingerprint)
        result = self.call("click", self.click_args())
        self.assertEqual(result["status"], "snapshot_expired")
        self.assertFalse(self.backend.calls)

    def test_unsafe_screenshot_coordinate_mapping_is_rejected(self):
        snapshot = {"screenshot": {"coordinates_usable": False}}
        with self.assertRaises(runtime.ToolError) as caught:
            runtime._resolve_action_point({"point": [1, 1], "space": "image"}, snapshot, None)
        self.assertEqual(caught.exception.code, "unsupported_action")

    def test_invalid_focus_mode_is_rejected_before_native_runtime(self):
        with self.assertRaises(runtime.ToolError) as caught:
            self.call("focus", {"window": self.window_ref})
        self.assertEqual(caught.exception.code, "background_unavailable")
        self.assertFalse(self.backend.calls)


if __name__ == "__main__":
    unittest.main()
