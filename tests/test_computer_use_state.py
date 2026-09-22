"""computer-use session 状态机的离线测试。"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/computer-use/scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]), \
     mock.patch.object(sys, "dont_write_bytecode", True), \
     mock.patch.dict(sys.modules, {"cua_driver": None}):
    import state


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = mock.patch.dict(os.environ, {state.STATE_ENV: str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = mock.patch.object(state.time, "time", return_value=1_000.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.session = "task-1"
        self.generation = state.load_session(self.session)["generation"]
        [self.window] = state.register_windows(self.session, [{
            "hwnd": 42, "pid": 7, "process_created": 99, "exe": "notepad.exe",
            "title": "Test", "bounding_box": {"left": 0, "top": 0, "right": 100, "bottom": 80},
        }])

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(state.ToolError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def save_snapshot(self):
        return state.save_snapshot(self.session, self.window["window_ref"], {
            "desktop_generation": 0,
            "elements": [{"name": "Save", "control_type": "Button", "window_name": "Test",
                          "bounding_box": {"left": 1, "top": 2, "right": 20, "bottom": 12}}],
        })

    def fingerprint(self, command="click", **args):
        return state.request_fingerprint(self.session, {
            "command": command,
            "args": args,
            "input_text": None,
        })

    def test_state_is_durable_and_successful_writes_are_complete_json(self):
        snapshot = self.save_snapshot()
        reloaded = state.load_session(self.session, create=False)
        saved = state.get_snapshot(self.session, snapshot["snapshot_ref"])
        self.assertEqual(saved["window_identity"]["hwnd"], 42)
        self.assertEqual(next(iter(saved["elements"].values()))["name"], "Save")
        files = list(self.root.rglob("*"))
        json_files = [path for path in files if path.suffix == ".json"]
        self.assertTrue(json_files)
        for path in json_files:
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
        self.assertEqual(reloaded["generation"], self.generation)
        self.assertFalse(any(path.name.endswith((".tmp", ".partial")) for path in files))

    def test_failed_atomic_commit_neither_consumes_snapshot_nor_records_request(self):
        snapshot = self.save_snapshot()
        request_id = str(uuid4())
        with mock.patch.object(state, "save_session", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.commit_dispatch(
                    self.session, request_id, "click", snapshot["snapshot_ref"],
                    self.fingerprint(snapshot=snapshot["snapshot_ref"]))
        reloaded = state.get_snapshot(
            self.session, snapshot["snapshot_ref"], for_action=True)
        self.assertFalse(reloaded["consumed"])
        self.assertIsNone(state.get_request_result(
            self.session, request_id, self.fingerprint(snapshot=snapshot["snapshot_ref"])))

    def test_snapshot_is_single_use_for_actions(self):
        snapshot = self.save_snapshot()
        request_id = str(uuid4())
        fingerprint = self.fingerprint(snapshot=snapshot["snapshot_ref"])
        state.commit_dispatch(self.session, request_id, "click", snapshot["snapshot_ref"], fingerprint)
        self.error("snapshot_consumed", state.get_snapshot,
                   self.session, snapshot["snapshot_ref"], for_action=True)
        self.error("snapshot_consumed", state.commit_dispatch,
                   self.session, str(uuid4()), "click", snapshot["snapshot_ref"], fingerprint)

    def test_same_request_id_returns_uncertain_or_saved_result_without_redispatch(self):
        snapshot = self.save_snapshot()
        request_id = str(uuid4())
        fingerprint = self.fingerprint(snapshot=snapshot["snapshot_ref"])
        state.commit_dispatch(self.session, request_id, "click", snapshot["snapshot_ref"], fingerprint)
        pending = state.get_request_result(self.session, request_id, fingerprint)
        self.assertEqual(pending["side_effect"], "uncertain")
        self.assertTrue(pending["data"]["deduplicated"])
        self.error("invalid_state", state.commit_dispatch,
                   self.session, request_id, "click", snapshot["snapshot_ref"], fingerprint)
        result = {"ok": True, "status": "dispatched", "side_effect": "dispatched", "data": {}}
        state.complete_request(self.session, request_id, result)
        replay = state.get_request_result(self.session, request_id, fingerprint)
        self.assertEqual(replay["status"], "dispatched")
        self.assertTrue(replay["data"]["deduplicated"])

    def test_request_id_cannot_be_reused_for_different_action(self):
        snapshot = self.save_snapshot()
        request_id = str(uuid4())
        fingerprint = self.fingerprint(snapshot=snapshot["snapshot_ref"])
        state.commit_dispatch(self.session, request_id, "click", snapshot["snapshot_ref"], fingerprint)
        different = self.fingerprint(command="type", snapshot=snapshot["snapshot_ref"])
        self.error("invalid_state", state.get_request_result,
                   self.session, request_id, different)
        self.error("invalid_state", state.commit_dispatch,
                   self.session, request_id, "type", None, different)

    def test_request_fingerprint_does_not_persist_sensitive_text(self):
        secret = "PRIVATE-TEXT-NOT-FOR-STATE"
        request = {"command": "type", "args": {"element": "e-example"}, "input_text": secret}
        fingerprint = state.request_fingerprint(self.session, request)
        state.commit_dispatch(self.session, str(uuid4()), "type", fingerprint=fingerprint)
        persisted = (self.root / "sessions" / self.session / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, persisted)
        self.assertNotIn(secret.encode("utf-8").hex(), persisted)
        self.assertIn(fingerprint, persisted)

    def test_expired_and_cross_session_snapshots_are_rejected(self):
        snapshot = self.save_snapshot()
        with mock.patch.object(state.time, "time", return_value=1_121.0):
            self.error("snapshot_expired", state.get_snapshot,
                       self.session, snapshot["snapshot_ref"], for_action=True)
        state.load_session("other")
        self.error("invalid_state", state.get_snapshot,
                   "other", snapshot["snapshot_ref"], for_action=True)

    def test_cleanup_closes_generation_and_invalidates_all_old_references(self):
        snapshot = self.save_snapshot()
        self.assertTrue(state.cleanup_session(self.session)["removed"])
        next_generation = state.load_session(self.session)["generation"]
        self.assertNotEqual(next_generation, self.generation)
        self.error("invalid_state", state.get_snapshot,
                   self.session, snapshot["snapshot_ref"], for_action=True)
        self.error("invalid_state", state.get_window,
                   self.session, self.window["window_ref"])

    def test_cleanup_is_scoped_to_one_session(self):
        state.load_session("other")
        [other_window] = state.register_windows("other", [{
            "hwnd": 2, "pid": 2, "process_created": 2, "exe": "other.exe",
        }])
        other_snapshot = state.save_snapshot("other", other_window["window_ref"], {"elements": []})
        state.cleanup_session(self.session)
        self.assertEqual(state.get_snapshot(
            "other", other_snapshot["snapshot_ref"])["window_ref"], other_window["window_ref"])

    def test_purge_rejects_reparse_sessions_root_before_iteration(self):
        with mock.patch.object(
            state, "_is_reparse_entry", side_effect=lambda path: path.name == "sessions"
        ):
            self.error("invalid_state", state.purge_expired, 100_000.0)


if __name__ == "__main__":
    unittest.main()
