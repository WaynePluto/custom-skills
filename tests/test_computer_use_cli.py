"""computer-use CLI 与一次性 worker 监督器的离线测试。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/computer-use/scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]), \
     mock.patch.object(sys, "dont_write_bytecode", True), \
     mock.patch.dict(sys.modules, {"cua_driver": None}):
    import computer


class CliTests(unittest.TestCase):
    def invoke(self, argv, stdin="", worker_result=None):
        worker_result = worker_result or {
            "schema_version": 1, "ok": True, "status": "completed",
            "request_id": None, "session": None, "side_effect": "none",
            "verified": False, "data": {}, "artifacts": [],
            "truncated": False, "elapsed_ms": 1,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(computer, "_run_worker", return_value=worker_result) as worker, \
             mock.patch.object(computer, "_read_stdin_with_deadline", return_value=stdin) as read_stdin, \
             mock.patch.object(computer.sys, "platform", "win32"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = computer.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), worker, read_stdin

    def assert_single_json(self, output):
        lines = output.splitlines()
        self.assertEqual(len(lines), 1, output)
        value = json.loads(lines[0])
        self.assertIsInstance(value, dict)
        return value

    def test_type_reads_body_only_from_stdin_and_emits_one_json(self):
        secret = "SECRET-正文-{ENTER}"
        request_id = str(uuid4())
        result = {
            "schema_version": 1, "ok": True, "status": "dispatched",
            "request_id": request_id, "session": "task", "side_effect": "dispatched",
            "verified": False, "data": {"input_length": len(secret)},
            "artifacts": [], "truncated": False, "elapsed_ms": 2,
        }
        code, stdout, stderr, worker, read_stdin = self.invoke([
            "--session", "task", "--request-id", request_id,
            "type", "--snapshot", "s1", "--element", "e1",
        ], stdin=secret, worker_result=result)
        self.assertEqual(code, 0)
        decoded = self.assert_single_json(stdout)
        self.assertEqual(decoded["data"]["input_length"], len(secret))
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        read_stdin.assert_called_once()
        request = worker.call_args.args[0]
        self.assertEqual(request["input_text"], secret)

    def test_type_has_no_command_line_text_argument(self):
        request_id = str(uuid4())
        code, stdout, stderr, worker, read_stdin = self.invoke([
            "--session", "task", "--request-id", request_id,
            "type", "--snapshot", "s1", "--element", "e1", "--text",
        ])
        self.assertNotEqual(code, 0)
        result = self.assert_single_json(stdout)
        self.assertEqual(result["status"], "invalid_argument")
        worker.assert_not_called()
        read_stdin.assert_not_called()

    def test_action_requires_request_uuid_but_read_only_command_does_not(self):
        code, stdout, _, worker, _ = self.invoke(["--session", "task", "windows"])
        self.assertEqual(code, 0)
        self.assertTrue(self.assert_single_json(stdout)["ok"])
        worker.assert_called_once()
        for argv in (["--session", "task", "click", "--snapshot", "s1", "--element", "e1"],
                     ["--session", "task", "--request-id", "not-a-uuid",
                      "click", "--snapshot", "s1", "--element", "e1"]):
            with self.subTest(argv=argv):
                code, stdout, _, worker, _ = self.invoke(argv)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.assert_single_json(stdout)["status"], "invalid_argument")
                worker.assert_not_called()

    def test_timeout_bounds_are_validated_before_worker(self):
        for timeout in ("0", "121", "nan"):
            with self.subTest(timeout=timeout):
                code, stdout, _, worker, _ = self.invoke(
                    ["--session", "task", "--timeout", timeout, "windows"])
                self.assertNotEqual(code, 0)
                self.assertEqual(self.assert_single_json(stdout)["status"], "invalid_argument")
                worker.assert_not_called()

    def test_parser_errors_also_use_stdout_json_not_argparse_prose(self):
        code, stdout, stderr, worker, _ = self.invoke(["--session", "../escape", "windows"])
        self.assertNotEqual(code, 0)
        result = self.assert_single_json(stdout)
        self.assertEqual(result["status"], "invalid_argument")
        self.assertFalse(stderr.strip().startswith("usage:"), stderr)
        worker.assert_not_called()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.job = mock.MagicMock()
        self.job.name = "fake-job"
        self.job.__enter__.return_value = self.job
        self.job.__exit__.return_value = None
        self.process = mock.Mock()
        self.process.returncode = 0
        self.process.wait.return_value = 0
        self.stack = mock.patch.multiple(
            computer,
            runtime_python=mock.DEFAULT,
            SupervisedJob=mock.DEFAULT,
        )
        patched = self.stack.start()
        self.addCleanup(self.stack.stop)
        patched["runtime_python"].return_value = Path(sys.executable)
        patched["SupervisedJob"].return_value = self.job
        self.popen = mock.patch.object(computer.subprocess, "Popen", return_value=self.process)
        self.spawn = self.popen.start()
        self.addCleanup(self.popen.stop)

    @staticmethod
    def request(command):
        return {
            "schema_version": 1, "command": command, "session": "task",
            "request_id": str(uuid4()) if command in computer.ACTION_COMMANDS else None,
            "timeout": 30, "args": {},
        }

    def run_worker(self, command, seconds=2):
        return computer._run_worker(self.request(command), time.monotonic() + seconds)

    def test_fake_runtime_returns_one_json_object(self):
        payload = {
            "schema_version": 1, "ok": True, "status": "completed", "request_id": None,
            "session": "task", "side_effect": "none", "verified": False,
            "data": {"count": 1}, "artifacts": [], "truncated": False, "elapsed_ms": 1,
        }
        self.process.communicate.return_value = (json.dumps(payload), "")
        self.assertEqual(self.run_worker("snapshot"), payload)
        self.job.assign.assert_called_once_with(self.process)
        self.process.communicate.assert_called_once()

    def test_timeout_maps_action_to_uncertain_and_never_retries(self):
        self.process.communicate.side_effect = subprocess.TimeoutExpired("worker", 0.1)
        with self.assertRaises(computer.ToolError) as caught:
            self.run_worker("click", seconds=0.1)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.side_effect, "uncertain")
        self.job.close.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=2)
        self.assertEqual(self.spawn.call_count, 1)

    def test_communication_error_after_send_preserves_uncertain_and_reaps_job(self):
        self.process.communicate.side_effect = OSError("pipe failure with SECRET")
        with self.assertRaises(computer.ToolError) as caught:
            self.run_worker("click")
        self.assertEqual(caught.exception.side_effect, "uncertain")
        self.assertNotIn("SECRET", str(caught.exception))
        self.job.close.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=2)

    def test_timeout_maps_read_only_worker_to_no_side_effect(self):
        self.process.communicate.side_effect = subprocess.TimeoutExpired("worker", 0.1)
        with self.assertRaises(computer.ToolError) as caught:
            self.run_worker("snapshot", seconds=0.1)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.side_effect, "none")
        self.assertEqual(self.spawn.call_count, 1)

    def test_worker_stderr_and_invalid_json_do_not_leak_sensitive_content(self):
        secret = "SECRET-WORKER-BODY"
        self.process.communicate.return_value = ("not-json", secret)
        with self.assertRaises(computer.ToolError) as caught:
            self.run_worker("click")
        encoded = json.dumps({
            "message": str(caught.exception), "details": caught.exception.details,
        }, ensure_ascii=False)
        self.assertEqual(caught.exception.code, "execution_error")
        self.assertEqual(caught.exception.side_effect, "uncertain")
        self.assertNotIn(secret, encoded)
        self.assertNotIn("not-json", encoded)


if __name__ == "__main__":
    unittest.main()
