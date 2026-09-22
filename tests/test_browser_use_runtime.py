"""Chrome 专用入口的离线回归测试；不依赖上游包、不启动浏览器。"""

import asyncio
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/browser-use/scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]), \
     mock.patch.object(sys, "dont_write_bytecode", True):
    import chrome as entry
    import chrome_host as host
    import chrome_runtime as runtime


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.patch = lambda target, **kwargs: self.stack.enter_context(mock.patch(target, **kwargs))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root), "BH_RUNTIME_DIR": str(self.root), "BU_NAME": "chrome",
        }, clear=True))
        self.spawn = self.patch("subprocess.Popen")
        self.spawn.return_value.pid = 456
        self.spawn.return_value.poll.return_value = None
        def publish_owner(*args, **kwargs):
            state = runtime.read_state(self.root)
            if state is not None:
                runtime.write_state(self.root, {**state, "owner": {"pid": 456, "started": 42}})
            return self.spawn.return_value
        self.spawn.side_effect = publish_owner
        self.process_run = self.patch("subprocess.run", side_effect=AssertionError("禁止真实子进程"))
        self.patch("socket.create_connection", side_effect=AssertionError("禁止联网"))
        self.patch("chrome_host._api", side_effect=AssertionError("禁止宿主系统调用"))
        self.admin = mock.Mock(spec=["daemon_alive", "_fingerprinted_pending_pid", "require_existing_daemon",
                                    "_publish_pid", "restart_daemon", "_pid_number", "_spawn_lock", "_process_start_time"])
        self.admin._process_start_time.return_value = 42
        self.admin.daemon_alive.return_value = False
        self.admin._fingerprinted_pending_pid.return_value = None
        self.ipc = types.SimpleNamespace(
            pid_path=mock.Mock(return_value=self.root / "chrome.pid"),
            log_path=mock.Mock(return_value=self.root / "chrome.log"),
            cleanup_endpoint=mock.Mock(), spawn_kwargs=mock.Mock(return_value={}),
            connect=mock.Mock(return_value=(mock.Mock(), "token")), request=mock.Mock(),
        )
        def reply(connection, token, request):
            state = runtime.read_state(self.root)
            if request["meta"] == "shutdown":
                self.admin._process_start_time.return_value = None
                return {"ok": True}
            return {"pong": True, "pid": state["owner"]["pid"], "adapter": runtime.ADAPTER,
                    "generation": state["generation"], "binding": state["binding"]}
        self.ipc.request.side_effect = reply
        self.endpoint = {"ws_url": "ws://127.0.0.1:9222/devtools/browser/test", "port": 9222,
                         "pid": 123, "started": 987654321}
        self.package = types.ModuleType("browser_harness")
        self.package.__path__ = []
        self.package.admin, self.package._ipc = self.admin, self.ipc
        self.stack.enter_context(mock.patch.dict(sys.modules, {"browser_harness": self.package}))

    def error(self, code, function, *args):
        with self.assertRaises(host.ChromeError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def state(self, phase="ready", endpoint=None):
        value = {"adapter": runtime.ADAPTER, "binding": runtime.binding(endpoint or self.endpoint), "phase": phase,
                 "generation": "a" * 32, "owner": {"pid": 456, "started": 42}}
        self.admin._process_start_time.return_value = 42
        runtime.write_state(self.root, value)
        return value

    def ensure(self, endpoint=None):
        return runtime.ensure_connection(self.root, endpoint or self.endpoint, self.admin, self.ipc)


class EntryTests(RuntimeCase):
    def test_isolated_env_overrides_connection_and_runtime_configuration(self):
        cleared = ("BU_CDP_WS", "BU_CDP_URL", "BU_BROWSER_ID", "BU_AUTOSPAWN", "BH_CHROME_PATH", "CHROME_PATH")
        dirty = dict.fromkeys((*cleared, "PYTHONPATH", "BU_NAME", "BH_RUNTIME_DIR", "BH_TMP_DIR",
                              "BH_CONFIG_DIR", "BH_AGENT_WORKSPACE"), "inherited")
        with mock.patch.dict(os.environ, dirty):
            before = dict(os.environ)
            env = entry.isolated_env("task-1")
            self.assertEqual(dict(os.environ), before)
        root = self.root / "custom-skills/browser-harness/task-1"
        self.assertEqual(env["BU_NAME"], "chrome")
        for key in (*cleared, "PYTHONPATH"):
            self.assertEqual(env[key], "", key)
        for key in ("BH_RUNTIME_DIR", "BH_TMP_DIR"):
            self.assertEqual(Path(env[key]), root)
        self.assertEqual(Path(env["BH_CONFIG_DIR"]), root / "config")
        self.assertEqual(Path(env["BH_AGENT_WORKSPACE"]), root / "workspace")
        for key in ("BH_RUNTIME_DIR_SHARED", "BH_TMP_DIR_SHARED", "BH_REQUIRE_EXISTING_DAEMON"):
            self.assertEqual(env[key], "1")
        for key in ("BH_DOMAIN_SKILLS", "BH_TAB_MARKER", "BH_RECORD", "BH_TELEMETRY"):
            self.assertEqual(env[key], "0")
        self.assertNotEqual(env["BH_RUNTIME_DIR"], entry.isolated_env("task-2")["BH_RUNTIME_DIR"])
        self.assertEqual(env["no_proxy"], "127.0.0.1,localhost,::1")
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])
        self.assertFalse(root.exists())

    def test_session_rejects_traversal_and_invalid_names(self):
        for name in ("", "..", "../escape", r"..\escape", "C:\\tmp", "a/b", "a b", "中文", "a" * 41):
            with self.subTest(name=name):
                self.error("invalid_session", entry.isolated_env, name)
        self.assertEqual(entry.isolated_env("A_0-" * 10)["BU_NAME"], "chrome")

    def test_runtime_python_only_locates_existing_install(self):
        python = self.root / "browser-harness/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        with mock.patch.dict(os.environ, {"UV_TOOL_DIR": str(self.root)}), \
             mock.patch.object(entry.shutil, "which", return_value=None):
            self.assertEqual(entry.runtime_python(), python.resolve())
        self.process_run.assert_not_called()
        with mock.patch.object(entry.shutil, "which", return_value="uv.exe"):
            self.process_run.side_effect = None
            self.process_run.return_value = mock.Mock(returncode=0, stdout=str(self.root))
            self.assertEqual(entry.runtime_python(), python.resolve())
        self.process_run.assert_called_once_with(["uv.exe", "tool", "dir"], capture_output=True, text=True, timeout=10)
        self.spawn.assert_not_called()

    def test_missing_runtime_never_installs_or_downloads(self):
        with mock.patch.object(entry.shutil, "which", return_value="uv.exe"):
            for failure in (OSError("missing"), entry.subprocess.TimeoutExpired("uv", 10)):
                with self.subTest(failure=failure):
                    self.process_run.side_effect = failure
                    self.error("runtime_missing", entry.runtime_python)
        self.assertTrue(all(call.args[0] == ["uv.exe", "tool", "dir"] for call in self.process_run.call_args_list))
        self.spawn.assert_not_called()

    def test_version_and_internal_entry_are_fail_closed(self):
        for value in ("0.1.13", "0.1.14", "0.1.13.dev1"):
            with self.subTest(version=value), mock.patch.object(runtime, "version", return_value=value):
                if value == "0.1.13":
                    self.assertEqual(runtime.check_version(), value)
                else:
                    self.error("incompatible_version", runtime.check_version)
        self.assertEqual(runtime.runtime_dir(), self.root)
        with mock.patch.dict(os.environ, {"BU_NAME": "other"}):
            self.error("invalid_entry", runtime.runtime_dir)


class ConnectionTests(RuntimeCase):
    def test_binding_tracks_process_fingerprint_and_websocket(self):
        original = runtime.binding(self.endpoint)
        self.assertEqual(original, runtime.binding(dict(reversed(list(self.endpoint.items())))))
        for key, value in (("pid", 999), ("started", 1), ("ws_url", "ws://127.0.0.1:9222/devtools/browser/new")):
            with self.subTest(key=key):
                self.assertNotEqual(original, runtime.binding({**self.endpoint, key: value}))
        self.assertNotIn(self.endpoint["ws_url"], original)

    def test_invalid_states_are_rejected_before_process_operations(self):
        self.admin.daemon_alive.return_value = True
        values = ("{", "null", "[]", '{"adapter":"other"}', json.dumps({"adapter": runtime.ADAPTER}),
                  json.dumps({"adapter": runtime.ADAPTER, "binding": 7, "phase": "ready"}),
                  json.dumps({"adapter": runtime.ADAPTER, "binding": runtime.binding(self.endpoint), "phase": "alien"}))
        for text in values:
            with self.subTest(state=text):
                (self.root / "chrome-entry.json").write_text(text, encoding="utf-8")
                self.error("invalid_state", self.ensure)
        self.spawn.assert_not_called()
        self.admin.restart_daemon.assert_not_called()

    def test_unknown_daemon_is_neither_adopted_nor_stopped(self):
        for alive, pending in ((True, None), (False, 456)):
            with self.subTest(alive=alive):
                self.admin.daemon_alive.return_value = alive
                self.admin._fingerprinted_pending_pid.return_value = pending
                self.error("unknown_daemon", self.ensure)
                self.error("unknown_daemon", runtime.stop_session, self.root, self.admin, self.ipc)
        self.admin.restart_daemon.assert_not_called()
        self.ipc.cleanup_endpoint.assert_not_called()
        self.spawn.assert_not_called()

    def test_old_state_cannot_adopt_a_different_ipc_daemon(self):
        state = self.state()
        self.admin.daemon_alive.return_value = True
        self.ipc.request.side_effect = None
        valid = {"pong": True, "pid": 456, "adapter": runtime.ADAPTER,
                 "generation": state["generation"], "binding": state["binding"]}
        for key, value in (("pid", 999), ("generation", "b" * 32), ("binding", "0" * 64), ("adapter", "other")):
            with self.subTest(key=key):
                self.ipc.request.return_value = {**valid, key: value}
                self.error("unknown_daemon", self.ensure)
                self.error("unknown_daemon", runtime.stop_session, self.root, self.admin, self.ipc)
        self.admin.restart_daemon.assert_not_called()
        self.admin.require_existing_daemon.assert_not_called()

    def test_owner_fingerprint_or_pending_pid_mismatch_is_rejected(self):
        self.state("starting")
        for pending, stamp in ((456, 43), (789, 42)):
            with self.subTest(pending=pending, stamp=stamp):
                self.admin._fingerprinted_pending_pid.return_value = pending
                self.admin._process_start_time.return_value = stamp
                self.error("unknown_daemon", self.ensure)
        self.spawn.assert_not_called()

    def test_missing_ipc_does_not_erase_a_live_owner_or_spawn_another(self):
        self.state()
        self.error("connection_lost", self.ensure)
        self.error("stop_failed", runtime.stop_session, self.root, self.admin, self.ipc)
        self.assertTrue((self.root / "chrome-entry.json").exists())
        self.spawn.assert_not_called()

    def test_ready_reuses_same_endpoint_and_health_checks_every_time(self):
        self.state()
        self.admin.daemon_alive.return_value = True
        for _ in range(2):
            self.assertEqual(self.ensure(), {"ok": True, "status": "ready"})
        self.assertEqual(self.admin.require_existing_daemon.call_args_list, [mock.call("chrome")] * 2)
        self.admin.require_existing_daemon.side_effect = RuntimeError("disconnected")
        self.error("connection_lost", self.ensure)
        self.spawn.assert_not_called()

    def test_pending_does_not_spawn_or_health_check_again(self):
        self.state("starting")
        self.admin._fingerprinted_pending_pid.return_value = 456
        for _ in range(2):
            self.assertEqual(self.ensure()["status"], "approval_pending")
        self.spawn.assert_not_called()
        self.admin.require_existing_daemon.assert_not_called()

    def test_changed_endpoint_is_rejected_for_live_and_pending_daemons(self):
        self.state()
        for alive, pending in ((True, None), (False, 456)):
            self.admin.daemon_alive.return_value = alive
            self.admin._fingerprinted_pending_pid.return_value = pending
            for key, value in (("pid", 999), ("started", 1), ("ws_url", "ws://127.0.0.1/other")):
                with self.subTest(alive=alive, key=key):
                    self.error("endpoint_changed", self.ensure, {**self.endpoint, key: value})
        self.spawn.assert_not_called()
        self.admin.require_existing_daemon.assert_not_called()

    def test_failed_or_abandoned_authorization_is_not_retried(self):
        for phase in ("starting", "failed"):
            with self.subTest(phase=phase):
                self.state(phase)
                self.admin._process_start_time.return_value = None
                self.error("connection_failed", self.ensure)
        self.spawn.assert_not_called()

    def test_cold_start_is_bounded_and_pending_endpoint_is_created_once(self):
        with mock.patch.object(runtime.time, "monotonic", side_effect=[0, 0, 1, 5]), \
             mock.patch.object(runtime.time, "sleep") as sleep:
            self.assertEqual(self.ensure()["status"], "approval_pending")
        self.assertEqual(sleep.call_args_list, [mock.call(0.1)] * 2)
        self.admin._fingerprinted_pending_pid.return_value = 456
        self.assertEqual(self.ensure()["status"], "approval_pending")
        self.spawn.assert_called_once()
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0], [sys.executable, "-I", "-X", "utf8", str(Path(runtime.__file__).resolve()), "daemon"])
        self.assertEqual(kwargs["stdin"], runtime.subprocess.DEVNULL)
        self.assertTrue(kwargs["stderr"].closed)
        self.admin._publish_pid.assert_called_once_with(self.root / "chrome.pid", 456)
        self.assertEqual(runtime.read_state(self.root)["phase"], "starting")

    def test_cold_ready_requires_health_and_failed_child_is_not_retried(self):
        for failure in (False, True):
            with self.subTest(health_failure=failure):
                (self.root / "chrome-entry.json").unlink(missing_ok=True)
                self.admin.daemon_alive.side_effect = [False, True]
                self.admin.require_existing_daemon.side_effect = RuntimeError("lost") if failure else None
                with mock.patch.object(runtime.time, "monotonic", side_effect=[0, 0]):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            self.ensure()
                    else:
                        self.assertEqual(self.ensure()["status"], "ready")
        self.assertEqual(self.admin.require_existing_daemon.call_count, 2)
        (self.root / "chrome-entry.json").unlink()
        self.admin.daemon_alive.side_effect = None
        self.spawn.return_value.poll.return_value = 1
        with mock.patch.object(runtime.time, "monotonic", side_effect=[0, 0]):
            self.error("connection_failed", self.ensure)
        count = self.spawn.call_count
        self.admin._process_start_time.return_value = None
        self.error("connection_failed", self.ensure)
        self.assertEqual(self.spawn.call_count, count)

    def test_stop_only_targets_own_named_daemon_and_detects_failure(self):
        self.state()
        self.admin.daemon_alive.side_effect = [True, False]
        self.assertEqual(runtime.stop_session(self.root, self.admin, self.ipc)["status"], "stopped")
        self.admin.restart_daemon.assert_not_called()
        self.assertEqual(self.ipc.request.call_args.args[-1], {"meta": "shutdown"})
        self.ipc.cleanup_endpoint.assert_called_once_with("chrome")
        self.assertTrue(all(call.args == ("chrome",) for call in self.ipc.pid_path.call_args_list))
        self.assertFalse((self.root / "chrome-entry.json").exists())
        self.state()
        self.admin.daemon_alive.side_effect = None
        self.admin.daemon_alive.return_value = True
        self.error("stop_failed", runtime.stop_session, self.root, self.admin, self.ipc)
        self.assertTrue((self.root / "chrome-entry.json").exists())
        self.spawn.assert_not_called()


    def test_stop_retries_windows_cleanup_race_only_after_process_exit(self):
        self.state()
        self.admin.daemon_alive.side_effect = [False, False]
        self.admin._fingerprinted_pending_pid.side_effect = [456, None]
        self.admin.restart_daemon.side_effect = PermissionError("port deleting")
        self.ipc.cleanup_endpoint.side_effect = [PermissionError("port deleting"), None]
        with mock.patch.object(runtime.time, "sleep") as sleep:
            self.assertTrue(runtime.stop_session(self.root, self.admin, self.ipc)["ok"])
        sleep.assert_called_once_with(0.1)
        self.assertFalse((self.root / "chrome-entry.json").exists())
        self.spawn.assert_not_called()

    def test_cleanup_permission_failure_keeps_state_and_is_bounded(self):
        self.state()
        self.admin._process_start_time.return_value = None
        self.ipc.cleanup_endpoint.side_effect = PermissionError("locked")
        with mock.patch.object(runtime.time, "monotonic", side_effect=[0, 2]):
            self.error("cleanup_pending", runtime.stop_session, self.root, self.admin, self.ipc)
        self.assertTrue((self.root / "chrome-entry.json").exists())

    def test_stop_pending_daemon_uses_only_own_name(self):
        self.state("starting")
        self.admin._fingerprinted_pending_pid.side_effect = [456, None]
        self.assertTrue(runtime.stop_session(self.root, self.admin, self.ipc)["ok"])
        self.admin.restart_daemon.assert_called_once_with("chrome")
        self.ipc.cleanup_endpoint.assert_called_once_with("chrome")

    def test_spawn_error_preserves_failed_attempt_without_automatic_retry(self):
        self.spawn.side_effect = OSError("spawn denied")
        with self.assertRaises(OSError):
            self.ensure()
        self.error("connection_failed", self.ensure)
        self.spawn.assert_called_once()


class DispatchTests(RuntimeCase):
    def test_doctor_checks_health_and_reports_pending_without_starting(self):
        self.state()
        with mock.patch.object(runtime, "find_chrome", return_value=self.root / "chrome.exe"), \
             mock.patch.object(runtime, "profile_root", return_value=self.root), \
             mock.patch.object(runtime, "probe_endpoint", return_value=self.endpoint), \
             mock.patch.object(runtime, "chrome_running", return_value=True), \
             mock.patch.object(runtime, "check_version", return_value="0.1.13"):
            for alive, pending, broken, expected in ((False, None, False, "daemon_idle"),
                    (False, 456, False, "approval_pending"), (True, None, False, "ready"),
                    (True, None, True, "connection_lost")):
                with self.subTest(expected=expected):
                    self.admin.daemon_alive.return_value = alive
                    self.admin._fingerprinted_pending_pid.return_value = pending
                    self.admin.require_existing_daemon.side_effect = RuntimeError("lost") if broken else None
                    self.assertEqual(runtime.dispatch("doctor", self.root, self.admin, self.ipc)["status"], expected)
        self.spawn.assert_not_called()

    def test_doctor_never_prepares_chrome_or_spawns(self):
        with mock.patch.object(runtime, "find_chrome", return_value=self.root / "chrome.exe"), \
             mock.patch.object(runtime, "profile_root", return_value=self.root), \
             mock.patch.object(runtime, "prepare_chrome") as prepare, \
             mock.patch.object(runtime, "probe_endpoint", return_value=None), \
             mock.patch.object(runtime, "chrome_running", return_value=False), \
             mock.patch.object(runtime, "check_version", return_value="0.1.13"):
            self.assertEqual(runtime.dispatch("doctor", self.root, self.admin, self.ipc)["status"], "chrome_closed")
        prepare.assert_not_called()
        self.spawn.assert_not_called()

    def test_doctor_does_not_treat_edge_as_chrome(self):
        edge = self.root / "msedge.exe"
        edge.touch()
        with mock.patch.object(host, "_windows"), \
             mock.patch.object(host, "_registry_candidates", return_value=[edge]):
            self.error("chrome_not_found", runtime.dispatch, "doctor", self.root, self.admin, self.ipc)
        self.spawn.assert_not_called()

    def test_execute_script_uses_official_runner_and_bounds_combined_output(self):
        run = types.ModuleType("browser_harness.run")
        self.package.run = run
        def execute(arguments):
            self.assertEqual(arguments, [])
            self.assertEqual(sys.stdin.read(), "never actually execute this")
            self.admin.require_existing_daemon.assert_called_once_with("chrome")
            print("x" * runtime.OUTPUT_LIMIT, end="")
            print("secret overflow", file=sys.stderr)
        run._run = mock.Mock(side_effect=execute)
        with mock.patch.object(sys, "stdin", io.StringIO()):
            result = runtime.execute_script("never actually execute this", self.admin)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["output"], "x" * runtime.OUTPUT_LIMIT)
        self.assertIsNone(run.print_update_banner())
        run._run.assert_called_once_with([])
        self.spawn.assert_not_called()

    def test_local_helper_is_injected_and_verifies_response_identity(self):
        state = self.state()
        identity = {"pong": True, "pid": 456, "adapter": runtime.ADAPTER,
                    "generation": state["generation"], "binding": state["binding"],
                    "session_tab_protocol": runtime.SESSION_TAB_PROTOCOL}
        tab = {"targetId": "dedicated", "url": "about:blank", "title": ""}
        self.ipc.request.side_effect = [identity, {**identity, "tab": tab}]
        run = types.ModuleType("browser_harness.run")
        self.package.run = run
        run._run = mock.Mock(side_effect=lambda args: print(run.session_tab()))
        with mock.patch.object(sys, "stdin", io.StringIO()):
            result = runtime.execute_script("unused", self.admin)
        self.assertTrue(result["ok"])
        self.assertIn("dedicated", result["output"])
        self.assertEqual(self.ipc.request.call_args.args[-1], {
            "meta": runtime.SESSION_TAB_META, "generation": state["generation"], "binding": state["binding"],
        })
        self.spawn.assert_not_called()

    def test_local_helper_rejects_legacy_daemon_and_changed_owner_without_reconnect(self):
        self.state()
        self.error("session_tab_unsupported", runtime.session_tab, self.root, self.admin, self.ipc)
        self.assertEqual(self.ipc.request.call_args_list[-1].args[-1], {"meta": "ping"})
        self.ipc.request.reset_mock()
        self.admin._process_start_time.return_value = 43
        self.error("unknown_daemon", runtime.session_tab, self.root, self.admin, self.ipc)
        self.ipc.request.assert_not_called()
        self.spawn.assert_not_called()
        self.admin.restart_daemon.assert_not_called()

    def test_real_helper_error_code_survives_script_boundary_and_output_truncation(self):
        self.state()
        run = types.ModuleType("browser_harness.run")
        self.package.run = run
        for overflow in (False, True):
            with self.subTest(overflow=overflow):
                def execute(args):
                    if overflow:
                        print("x" * runtime.OUTPUT_LIMIT, end="")
                    run.session_tab()
                run._run = mock.Mock(side_effect=execute)
                with mock.patch.object(sys, "stdin", io.StringIO()):
                    result = runtime.execute_script("unused", self.admin)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "script_failed")
                self.assertEqual(result["error_code"], "session_tab_unsupported")
                self.assertEqual(result["truncated"], overflow)
                if not overflow:
                    self.assertIn("[session_tab_unsupported]", result["output"])
        self.spawn.assert_not_called()

    def test_runner_errors_and_health_failure_do_not_execute_real_code(self):
        self.package.run = types.SimpleNamespace(_run=mock.Mock())
        for exception, ok in ((SystemExit(0), True), (SystemExit(2), False), (ValueError("bad"), False)):
            with self.subTest(exception=exception), mock.patch.object(sys, "stdin", io.StringIO()):
                self.package.run._run.side_effect = exception
                result = runtime.execute_script("unused", self.admin)
                self.assertEqual(result["ok"], ok)
                self.assertEqual(result["status"], "completed" if ok else "script_failed")
        self.package.run._run.reset_mock()
        self.admin.require_existing_daemon.side_effect = RuntimeError("lost")
        with self.assertRaises(RuntimeError):
            runtime.execute_script("unused", self.admin)
        self.package.run._run.assert_not_called()


class PinnedDaemonTests(RuntimeCase):
    def fake_daemon(self, target="dedicated"):
        instances = []
        module = types.SimpleNamespace(get_ws_url=mock.Mock(side_effect=AssertionError("禁止自动发现")),
                                       BROWSER_KIND="remote", REMOTE_ID="cloud", PROFILES=[])
        test = self
        class Daemon:
            def __init__(self):
                instances.append(self)
                self.cdp = types.SimpleNamespace(send_raw=mock.AsyncMock(), stop=mock.AsyncMock())
                self.dedicated_target_id = target
                self.session = "original-session"
            async def handle(self, request):
                if request.get("token") == "invalid":
                    return {"error": "unauthorized"}
                return {"pong": True, "pid": 456}
            async def _close_inspect_tabs(self, targets):
                raise AssertionError("禁止关闭用户 inspect 页")
            async def start(self):
                test.assertEqual(module.BROWSER_KIND, "local")
                test.assertIsNone(module.REMOTE_ID)
                test.assertEqual(module.get_ws_url(), test.endpoint["ws_url"])
                await self._close_inspect_tabs([{"targetId": "user-inspect", "url": "chrome://inspect/"}])
        module.Daemon, module.serve = Daemon, mock.AsyncMock()
        self.patch("chrome_runtime.find_chrome", return_value=self.root / "chrome.exe")
        self.patch("chrome_runtime.profile_root", return_value=self.root)
        self.probe = self.patch("chrome_runtime.probe_endpoint", return_value=self.endpoint)
        return module, instances

    def test_pinned_local_handshake_preserves_inspect_and_closes_only_owned_target(self):
        daemon, instances = self.fake_daemon()
        with mock.patch.object(runtime.asyncio, "wait_for", wraps=asyncio.wait_for) as wait:
            asyncio.run(runtime.serve_pinned(self.root, self.state("starting"), daemon))
        connection = instances[0]
        self.assertEqual(daemon.PROFILES, [self.root])
        response = asyncio.run(connection.handle({"meta": "ping"}))
        self.assertEqual(response["generation"], "a" * 32)
        self.assertEqual(response["binding"], runtime.binding(self.endpoint))
        self.assertEqual(response["adapter"], runtime.ADAPTER)
        self.assertEqual(daemon.get_ws_url(), self.endpoint["ws_url"])
        self.probe.assert_called_once()
        self.assertEqual([call.args[1] for call in wait.call_args_list], [180, 2, 2])
        daemon.serve.assert_awaited_once_with(connection)
        connection.cdp.send_raw.assert_awaited_once_with("Target.closeTarget", {"targetId": "dedicated"})
        connection.cdp.stop.assert_awaited_once()
        self.assertEqual(runtime.read_state(self.root)["phase"], "ready")

    def test_closed_page_commands_do_not_enter_upstream_implicit_session_recovery(self):
        daemon, instances = self.fake_daemon()
        asyncio.run(runtime.serve_pinned(self.root, self.state("starting"), daemon))
        connection = instances[0]
        forwarded, recovered = [], []
        async def stale_handler(instance, request):
            forwarded.append(request)
            if not request.get("session_id"):
                recovered.append("created replacement page and replayed command")
                return {"result": {}}
            return {"error": "Session with given id not found"}
        # 覆盖切换时清理标题与首导航两处关闭窗口；显式 session 走上游失败分支。
        with mock.patch.object(daemon.Daemon, "handle", new=stale_handler):
            for method in ("Runtime.evaluate", "Page.navigate"):
                request = {"method": method, "params": {}}
                result = asyncio.run(connection.handle(request))
                self.assertIn("error", result)
                self.assertNotIn("session_id", request)
        self.assertEqual(recovered, [])
        self.assertEqual([request["session_id"] for request in forwarded], ["original-session"] * 2)

    def test_target_commands_and_explicit_sessions_are_not_rewritten(self):
        daemon, instances = self.fake_daemon()
        asyncio.run(runtime.serve_pinned(self.root, self.state("starting"), daemon))
        connection = instances[0]
        base = mock.AsyncMock(return_value={"result": {}})
        with mock.patch.object(daemon.Daemon, "handle", base):
            for request in ({"method": "Target.getTargets"},
                            {"method": "Runtime.evaluate", "session_id": "explicit-frame"},
                            {"meta": "current_tab"}):
                asyncio.run(connection.handle(request))
                base.assert_awaited_with(request)
            base.reset_mock()
            connection.session = None
            result = asyncio.run(connection.handle({"method": "Page.navigate"}))
            self.assertEqual(result, {"error": "not_attached"})
            base.assert_not_awaited()

    def test_private_session_tab_meta_reuses_upstream_authorization(self):
        daemon, instances = self.fake_daemon()
        state = self.state("starting")
        asyncio.run(runtime.serve_pinned(self.root, state, daemon))
        connection = instances[0]
        connection.cdp.send_raw.reset_mock()
        request = {"meta": runtime.SESSION_TAB_META, "generation": state["generation"],
                   "binding": state["binding"], "token": "invalid"}
        result = asyncio.run(connection.handle(request))
        self.assertEqual(result, {"error": "unauthorized"})
        connection.cdp.send_raw.assert_not_awaited()
        connection.cdp.send_raw.return_value = {"targetInfo": {
            "targetId": "dedicated", "type": "page", "url": "about:blank", "title": "",
        }}
        result = asyncio.run(connection.handle({**request, "token": "valid"}))
        self.assertEqual(result["tab"]["targetId"], "dedicated")
        self.assertEqual(result["session_tab_protocol"], runtime.SESSION_TAB_PROTOCOL)
        self.assertEqual(result["generation"], state["generation"])
        connection.cdp.send_raw.assert_awaited_once_with("Target.getTargetInfo", {"targetId": "dedicated"})

    def test_changed_or_missing_endpoint_never_begins_handshake(self):
        daemon, instances = self.fake_daemon()
        for endpoint in (None, {**self.endpoint, "started": 0}):
            with self.subTest(endpoint=endpoint):
                self.probe.return_value = endpoint
                self.error("endpoint_changed", asyncio.run, runtime.serve_pinned(self.root, self.state(), daemon))
        self.assertEqual(instances, [])
        daemon.serve.assert_not_awaited()
        daemon.get_ws_url.assert_not_called()

    def test_approval_timeout_releases_client_without_closing_user_targets(self):
        daemon, instances = self.fake_daemon(target=None)
        original = asyncio.wait_for
        async def timeout(awaitable, seconds):
            if seconds == 180:
                awaitable.close()
                raise asyncio.TimeoutError("authorization pending")
            return await original(awaitable, seconds)
        with mock.patch.object(runtime.asyncio, "wait_for", side_effect=timeout):
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(runtime.serve_pinned(self.root, self.state("starting"), daemon))
        daemon.serve.assert_not_awaited()
        instances[0].cdp.send_raw.assert_not_awaited()
        instances[0].cdp.stop.assert_awaited_once()
        self.assertEqual(runtime.read_state(self.root)["phase"], "starting")

    def test_daemon_failure_records_phase_and_only_cleans_own_pid(self):
        daemon, _ = self.fake_daemon()
        self.package.daemon = daemon
        daemon._publish_own_pid, daemon.log = mock.Mock(), mock.Mock()
        for pid, cleaned in ((os.getpid(), True), (99999999, False)):
            with self.subTest(pid=pid):
                self.state("starting")
                self.admin._pid_number.return_value = pid
                self.ipc.cleanup_endpoint.reset_mock()
                with mock.patch.object(runtime, "serve_pinned", new=mock.AsyncMock(side_effect=TimeoutError())):
                    self.assertEqual(runtime.daemon_main(self.root, self.admin, self.ipc), 1)
                self.assertEqual(runtime.read_state(self.root)["phase"], "failed")
                self.assertEqual(self.ipc.cleanup_endpoint.called, cleaned)
        self.assertNotIn(self.endpoint["ws_url"], str(daemon.log.call_args_list))

    def test_serve_and_close_errors_still_release_client(self):
        daemon, instances = self.fake_daemon()
        async def fail(connection):
            connection.cdp.send_raw.side_effect = RuntimeError("target disappeared")
            connection.cdp.stop.side_effect = RuntimeError("already closed")
            raise ValueError("serve failed")
        daemon.serve.side_effect = fail
        with self.assertRaisesRegex(ValueError, "serve failed"):
            asyncio.run(runtime.serve_pinned(self.root, self.state("starting"), daemon))
        instances[0].cdp.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
