"""Chrome 宿主探测的离线测试，不启动浏览器、不访问网络。"""

import ctypes
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "skills/browser-use/scripts/chrome_host.py"
SPEC = importlib.util.spec_from_file_location("chrome_host_under_test", MODULE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class HostTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.binary = self.base / "chrome.exe"
        self.binary.write_bytes(b"")
        self.root = self.base / "User Data"
        self.root.mkdir()
        self.platform = mock.patch.object(host, "_windows")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        launcher = mock.patch.object(host.subprocess, "Popen")
        self.launch = launcher.start()
        self.addCleanup(launcher.stop)
        self.endpoint = {"ws_url": "ws://127.0.0.1:45678/devtools/browser/abc-123", "port": 45678,
                         "pid": 101, "started": 123456789}

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(host.ChromeError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def port_file(self, content=b"45678\n/devtools/browser/abc-123\n"):
        (self.root / "DevToolsActivePort").write_bytes(content)

    def local_state(self, name):
        (self.root / "Local State").write_text(json.dumps({"profile": {"last_used": name}}), encoding="utf-8")


class DiscoveryTests(HostTestCase):
    def test_unsupported_platform_is_explicit(self):
        self.platform.stop()
        with mock.patch.object(host.sys, "platform", "linux"):
            for function, args in ((host.find_chrome, ()), (host.profile_root, ()),
                                   (host.chrome_running, (self.binary,)),
                                   (host.probe_endpoint, (self.binary, self.root)),
                                   (host.prepare_chrome, (self.binary, self.root))):
                self.error("unsupported_platform", function, *args)
        self.launch.assert_not_called()

    def test_registry_chrome_is_selected(self):
        with mock.patch.object(host, "_registry_candidates", return_value=[self.binary]):
            self.assertEqual(host.find_chrome(), self.binary.resolve())

    def test_common_install_location_is_selected(self):
        chrome = self.base / "Google/Chrome/Application/chrome.exe"
        chrome.parent.mkdir(parents=True)
        chrome.write_bytes(b"")
        with mock.patch.object(host, "_registry_candidates", return_value=[]), \
             mock.patch.dict(os.environ, {"PROGRAMFILES": str(self.base)}, clear=True):
            self.assertEqual(host.find_chrome(), chrome.resolve())

    def test_edge_only_does_not_find_chrome(self):
        edge = self.base / "msedge.exe"
        edge.write_bytes(b"")
        with mock.patch.object(host, "_registry_candidates", return_value=[edge]), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.error("chrome_not_found", host.find_chrome)

    def test_missing_and_non_stable_candidates_are_ignored(self):
        candidates = [self.base / "absent/chrome.exe"]
        for channel in ("Chrome Beta", "Chrome Dev", "Chrome SxS", "Chromium"):
            candidate = self.base / channel / "chrome.exe"
            candidate.parent.mkdir()
            candidate.write_bytes(b"")
            candidates.append(candidate)
        with mock.patch.object(host, "_registry_candidates", return_value=candidates), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.error("chrome_not_found", host.find_chrome)

    def test_registry_checks_both_hives_and_views(self):
        registry = mock.MagicMock()
        registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE = 1, 2
        registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY, registry.KEY_READ = 256, 512, 1
        registry.REG_SZ, registry.REG_EXPAND_SZ = 1, 2
        registry.QueryValueEx.return_value = (f'"{self.binary}"', 1)
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            self.assertEqual(list(host._registry_candidates()), [self.binary] * 4)
        self.assertEqual([(call.args[0], call.args[3]) for call in registry.OpenKey.call_args_list],
                         [(1, 257), (1, 513), (2, 257), (2, 513)])

    def test_registry_permission_error_is_not_silenced(self):
        registry = mock.MagicMock()
        registry.OpenKey.side_effect = PermissionError("denied")
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            self.error("chrome_lookup_failed", lambda: list(host._registry_candidates()))

    def test_profile_root_uses_personal_local_app_data(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}, clear=True):
            self.assertEqual(host.profile_root(), self.base / "Google/Chrome/User Data")

    def test_missing_or_relative_profile_root_is_rejected(self):
        for value in ("", "relative"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"LOCALAPPDATA": value}, clear=True):
                self.error("profile_not_found", host.profile_root)


class RunningTests(HostTestCase):
    def test_process_must_match_exact_binary(self):
        other = self.base / "other/chrome.exe"
        for image, expected in ((self.binary, True), (other, False), (self.base / "msedge.exe", False),
                                (self.base / "msedgewebview2.exe", False)):
            with self.subTest(image=image), mock.patch.object(host, "_chrome_pids", return_value=[101]), \
                 mock.patch.object(host, "_process_identity", return_value=(image, 123)):
                self.assertEqual(host.chrome_running(self.binary), expected)

    def test_exited_process_is_not_running(self):
        with mock.patch.object(host, "_chrome_pids", return_value=[101]), \
             mock.patch.object(host, "_process_identity", return_value=None):
            self.assertFalse(host.chrome_running(self.binary))

    def test_query_failure_is_not_false(self):
        for operation in ("_chrome_pids", "_process_identity"):
            with self.subTest(operation=operation), mock.patch.object(host, "_chrome_pids", return_value=[101]), \
                 mock.patch.object(host, operation, side_effect=host.ChromeError("permission_denied", "denied")):
                self.error("permission_denied", host.chrome_running, self.binary)

    def test_edge_only_enumeration_never_queries_edge(self):
        names = iter([(1, "msedge.exe"), (2, "msedgewebview2.exe")])

        def advance(handle, pointer):
            row = next(names, None)
            if row is None:
                return False
            pointer._obj.pid, pointer._obj.name = row
            return True

        close = mock.Mock()
        functions = {"CreateToolhelp32Snapshot": lambda *args: 99,
                     "Process32FirstW": advance, "Process32NextW": advance, "CloseHandle": close}
        with mock.patch.object(host, "_api", side_effect=lambda library, name, *args: functions[name]), \
             mock.patch.object(ctypes, "get_last_error", return_value=18, create=True), \
             mock.patch.object(host, "_process_identity") as identity:
            self.assertFalse(host.chrome_running(self.binary))
        identity.assert_not_called()
        close.assert_called_once_with(99)


class EndpointTests(HostTestCase):
    def test_missing_file_does_not_scan_any_port(self):
        with mock.patch.object(host, "_listener_pids") as listeners:
            self.assertIsNone(host.probe_endpoint(self.binary, self.root))
        listeners.assert_not_called()

    def test_valid_endpoint_has_bound_identity(self):
        self.port_file()
        with mock.patch.object(host, "_listener_pids", return_value={101}) as listeners, \
             mock.patch.object(host, "_process_identity", return_value=(self.binary, 123456789)):
            self.assertEqual(host.probe_endpoint(self.binary, self.root), self.endpoint)
        self.assertEqual(listeners.call_args_list, [mock.call(45678), mock.call(45678)])

    def test_crlf_and_port_boundaries(self):
        for port in (1, 65535):
            self.port_file(f"{port}\r\n/devtools/browser/abc_123-DEF\r\n".encode())
            self.assertEqual(host._read_endpoint(self.root), (port, "/devtools/browser/abc_123-DEF"))

    def test_malformed_content_is_rejected_without_port_query(self):
        values = [b"", b"0\n/devtools/browser/id", b"65536\n/devtools/browser/id",
                  b"-1\n/devtools/browser/id", b"12345", b"9222\nhttp://evil/devtools/browser/id",
                  b"9222\n/devtools/page/id", b"9222\n/devtools/browser/",
                  b"9222\n/devtools/browser/../id", b"9222\n/devtools/browser/id?x=1",
                  b"9222\n/devtools/browser/id#fragment", b"9222\n/devtools/browser/id\nextra",
                  b"9222\n/devtools/browser/id\x00", b"9222\n/devtools/browser/\xff", b"x" * 4097]
        with mock.patch.object(host, "_listener_pids") as listeners:
            for value in values:
                with self.subTest(value=value[:100]):
                    self.port_file(value)
                    self.error("invalid_endpoint", host.probe_endpoint, self.binary, self.root)
        listeners.assert_not_called()

    def test_stale_port_or_exited_owner_is_none(self):
        self.port_file()
        for owners in (set(), {101}):
            with self.subTest(owners=owners), mock.patch.object(host, "_listener_pids", return_value=owners), \
                 mock.patch.object(host, "_process_identity", return_value=None):
                self.assertIsNone(host.probe_endpoint(self.binary, self.root))

    def test_other_process_or_other_chrome_installation_is_rejected(self):
        self.port_file()
        for binary in (self.base / "msedge.exe", self.base / "msedgewebview2.exe", self.base / "other/chrome.exe"):
            with self.subTest(binary=binary), mock.patch.object(host, "_listener_pids", return_value={101}), \
                 mock.patch.object(host, "_process_identity", return_value=(binary, 123)):
                self.error("endpoint_mismatch", host.probe_endpoint, self.binary, self.root)

    def test_ambiguous_or_changing_owner_is_rejected(self):
        self.port_file()
        for sequence in (({0},), ({101, 102},), ({101}, {102}), ({101}, set())):
            with self.subTest(sequence=sequence), mock.patch.object(host, "_listener_pids", side_effect=sequence), \
                 mock.patch.object(host, "_process_identity", return_value=(self.binary, 123)):
                self.error("endpoint_mismatch", host.probe_endpoint, self.binary, self.root)

    def test_file_permission_error_is_explicit(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.error("endpoint_read_failed", host.probe_endpoint, self.binary, self.root)

    def test_owner_permission_error_is_explicit(self):
        self.port_file()
        with mock.patch.object(host, "_listener_pids", return_value={101}), \
             mock.patch.object(host, "_process_identity", side_effect=host.ChromeError("permission_denied", "denied")):
            self.error("permission_denied", host.probe_endpoint, self.binary, self.root)


class NativeQueryTests(HostTestCase):
    def test_process_identity_queries_path_and_creation_time_and_closes_handle(self):
        close = mock.Mock()

        def image(handle, flags, buffer, size):
            buffer.value = str(self.binary)
            return True

        def times(handle, created, *unused):
            created._obj.dwHighDateTime, created._obj.dwLowDateTime = 7, 123
            return True

        functions = {"OpenProcess": mock.Mock(return_value=99), "QueryFullProcessImageNameW": image,
                     "GetProcessTimes": times, "CloseHandle": close}
        with mock.patch.object(host, "_api", side_effect=lambda library, name, *args: functions[name]):
            self.assertEqual(host._process_identity(101), (self.binary, (7 << 32) | 123))
        functions["OpenProcess"].assert_called_once_with(0x1000, False, 101)
        close.assert_called_once_with(99)

    def test_missing_and_inaccessible_process_are_distinguished(self):
        for number in (87, 5):
            with self.subTest(number=number), mock.patch.object(host, "_api", return_value=lambda *args: None), \
                 mock.patch.object(ctypes, "get_last_error", return_value=number, create=True):
                if number == 87:
                    self.assertIsNone(host._process_identity(101))
                else:
                    self.error("permission_denied", host._process_identity, 101)

    def test_query_failure_always_closes_handle(self):
        for failing in ("QueryFullProcessImageNameW", "GetProcessTimes"):
            close = mock.Mock()
            functions = {"OpenProcess": lambda *args: 99, "QueryFullProcessImageNameW": lambda *args: True,
                         "GetProcessTimes": lambda *args: True, "CloseHandle": close}
            functions[failing] = lambda *args: False
            with self.subTest(failing=failing), \
                 mock.patch.object(host, "_api", side_effect=lambda library, name, *args: functions[name]), \
                 mock.patch.object(ctypes, "get_last_error", return_value=5, create=True):
                self.error("permission_denied", host._process_identity, 101)
            close.assert_called_once_with(99)

    def test_snapshot_failure_is_explicit(self):
        with mock.patch.object(host, "_api", return_value=lambda *args: ctypes.c_void_p(-1).value), \
             mock.patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.error("permission_denied", host._chrome_pids)

    @unittest.skipUnless(sys.platform == "win32", "仅 Windows 使用原生结构布局")
    def test_tcp_table_only_accepts_matching_ipv4_listeners(self):
        rows = []
        for address, port, pid, state in (("127.0.0.1", 45678, 101, 2), ("0.0.0.0", 45678, 102, 2),
                                           ("192.168.0.1", 45678, 103, 2), ("127.0.0.1", 9222, 104, 2),
                                           ("127.0.0.1", 45678, 105, 5)):
            row = host._TcpRow()
            row.address = int.from_bytes(socket.inet_aton(address), "little")
            row.port, row.pid, row.state = socket.htons(port), pid, state
            rows.append(bytes(row))
        payload = len(rows).to_bytes(4, "little") + b"".join(rows)

        def query(buffer, size, ordered, family, kind, reserved):
            self.assertEqual((family, kind), (socket.AF_INET, 3))
            size._obj.value = len(payload)
            if buffer is None:
                return 122
            ctypes.memmove(buffer, payload, len(payload))
            return 0

        with mock.patch.object(host, "_api", return_value=query):
            self.assertEqual(host._listener_pids(45678), {101, 102})

    def test_tcp_query_permissions_and_growth_are_bounded(self):
        for status, code, calls in ((5, "permission_denied", 1), (122, "process_query_failed", 4)):
            query = mock.Mock()

            def result(buffer, size, *unused):
                size._obj.value = 64
                return status

            query.side_effect = result
            with self.subTest(status=status), mock.patch.object(host, "_api", return_value=query):
                self.error(code, host._listener_pids, 45678)
            self.assertEqual(query.call_count, calls)


class PrepareTests(HostTestCase):
    def test_existing_endpoint_reused_without_process_scan_or_launch(self):
        with mock.patch.object(host, "probe_endpoint", return_value=self.endpoint), \
             mock.patch.object(host, "chrome_running") as running:
            self.assertEqual(host.prepare_chrome(self.binary, self.root), self.endpoint)
        running.assert_not_called()
        self.launch.assert_not_called()

    def test_running_chrome_without_endpoint_is_not_restarted(self):
        with mock.patch.object(host, "probe_endpoint", return_value=None), \
             mock.patch.object(host, "chrome_running", return_value=True), mock.patch.object(host.time, "sleep") as sleep:
            message = self.error("setup_required", host.prepare_chrome, self.binary, self.root)
        self.assertIn("chrome://inspect/#remote-debugging", message)
        self.launch.assert_not_called()
        sleep.assert_not_called()

    def test_closed_chrome_is_launched_once_with_only_personal_profile(self):
        self.local_state("Profile 2")
        (self.root / "Profile 2").mkdir()
        with mock.patch.object(host, "probe_endpoint", side_effect=[None, self.endpoint]), \
             mock.patch.object(host, "chrome_running", return_value=False):
            self.assertEqual(host.prepare_chrome(self.binary, self.root), self.endpoint)
        self.launch.assert_called_once_with([str(self.binary.resolve()), "--profile-directory=Profile 2"],
                                            stdin=host.subprocess.DEVNULL, stdout=host.subprocess.DEVNULL,
                                            stderr=host.subprocess.DEVNULL)

    def test_invalid_or_absent_profile_is_safely_ignored(self):
        for name in ("../escape", "..\\escape", "C:\\escape", "--remote-debugging-port=9222", "x\" y",
                     "foo/bar", "foo ", "", None, [], "Missing Profile"):
            with self.subTest(name=name):
                self.local_state(name)
                self.assertIsNone(host._last_profile(self.root))

    def test_invalid_json_and_permission_error_do_not_select_profile(self):
        (self.root / "Local State").write_bytes(b"not json")
        self.assertIsNone(host._last_profile(self.root))
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(host._last_profile(self.root))

    def test_profile_without_state_launches_without_extra_flags(self):
        with mock.patch.object(host, "probe_endpoint", side_effect=[None, self.endpoint]), \
             mock.patch.object(host, "chrome_running", return_value=False):
            host.prepare_chrome(self.binary, self.root)
        self.assertEqual(self.launch.call_args.args, ([str(self.binary.resolve())],))

    def test_wait_is_bounded_and_does_not_launch_again(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(host, "probe_endpoint", return_value=None), \
             mock.patch.object(host, "chrome_running", return_value=False), \
             mock.patch.object(host.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(host.time, "sleep", side_effect=sleep):
            self.error("setup_required", host.prepare_chrome, self.binary, self.root, 0.55)
        self.assertAlmostEqual(clock[0], 0.55)
        self.assertTrue(all(0 < seconds <= 0.2 for seconds in sleeps))
        self.launch.assert_called_once()

    def test_nonfinite_or_negative_wait_never_launches(self):
        for seconds in (-1, float("nan"), float("inf")):
            self.error("invalid_argument", host.prepare_chrome, self.binary, self.root, seconds)
        self.launch.assert_not_called()

    def test_query_failure_never_triggers_launch(self):
        for operation in ("probe_endpoint", "chrome_running"):
            with self.subTest(operation=operation), mock.patch.object(host, "probe_endpoint", return_value=None), \
                 mock.patch.object(host, operation, side_effect=host.ChromeError("permission_denied", "denied")):
                self.error("permission_denied", host.prepare_chrome, self.binary, self.root)
        self.launch.assert_not_called()

    def test_launch_permission_error_is_explicit(self):
        self.launch.side_effect = PermissionError("denied")
        with mock.patch.object(host, "probe_endpoint", return_value=None), \
             mock.patch.object(host, "chrome_running", return_value=False):
            self.error("launch_failed", host.prepare_chrome, self.binary, self.root)
        self.launch.assert_called_once()


class SafetyBoundaryTests(HostTestCase):
    def test_binary_alias_must_resolve_to_chrome_basename(self):
        with mock.patch.object(Path, "resolve", return_value=self.base / "msedge.exe"):
            self.error("chrome_not_found", host._binary, self.binary)

    def test_binary_permission_failure_is_explicit(self):
        with mock.patch.object(Path, "resolve", side_effect=PermissionError("denied")):
            self.error("chrome_lookup_failed", host._binary, self.binary)

    def test_profile_junction_outside_root_is_ignored(self):
        folder = self.root / "Profile 1"
        folder.mkdir()
        self.local_state("Profile 1")
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            return self.base / "outside" if path == folder else original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertIsNone(host._last_profile(self.root))

    def test_zero_wait_never_sleeps_or_relaunches(self):
        with mock.patch.object(host, "probe_endpoint", return_value=None) as probe, \
             mock.patch.object(host, "chrome_running", return_value=False), mock.patch.object(host.time, "sleep") as sleep:
            self.error("setup_required", host.prepare_chrome, self.binary, self.root, 0)
        self.launch.assert_called_once()
        probe.assert_called_once()
        sleep.assert_not_called()

    def test_endpoint_appearing_during_wait_is_returned(self):
        with mock.patch.object(host, "probe_endpoint", side_effect=[None, None, self.endpoint]), \
             mock.patch.object(host, "chrome_running", return_value=False), mock.patch.object(host.time, "sleep") as sleep:
            self.assertEqual(host.prepare_chrome(self.binary, self.root), self.endpoint)
        self.launch.assert_called_once()
        sleep.assert_called_once()

if __name__ == "__main__":
    unittest.main()
