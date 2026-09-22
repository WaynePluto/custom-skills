#!/usr/bin/env python3
"""install.py computer-use 安装流程的离线单元测试。"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install  # noqa: E402


class ComputerUseRuntimeDirTest(unittest.TestCase):
    """运行时路径必须可隔离，且拒绝相对路径。"""

    def test_environment_variable_selects_absolute_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            with mock.patch.dict(
                install.os.environ,
                {install.COMPUTER_USE_RUNTIME_ENV: str(runtime)},
                clear=False,
            ):
                self.assertEqual(install.computer_use_runtime_dir(), runtime)
                self.assertTrue(install.computer_use_runtime_dir().is_absolute())

    def test_environment_variable_rejects_relative_runtime(self):
        with mock.patch.dict(
            install.os.environ,
            {install.COMPUTER_USE_RUNTIME_ENV: "relative/runtime"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "必须是绝对路径"):
                install.computer_use_runtime_dir()

    def test_localappdata_fallback_is_absolute(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            environment = {"LOCALAPPDATA": str(local)}
            with mock.patch.dict(install.os.environ, environment, clear=True):
                self.assertEqual(
                    install.computer_use_runtime_dir(),
                    local / "custom-skills" / "computer-use" / "runtime",
                )


class VerifyComputerUseRuntimeTest(unittest.TestCase):
    """校验器只执行隔离的 backend.py，并解析有界 JSON 结果。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.backend = self.source / "scripts" / "backend.py"
        self.backend.parent.mkdir(parents=True)
        self.backend.write_text("raise SystemExit('must be mocked')\n", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.python = install.computer_use_runtime_python(self.runtime)
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"not a real interpreter")

    def test_successfully_parses_result_and_uses_isolated_backend_command(self):
        payload = {
            "ok": True,
            "cua_driver": "0.28.2",
            "native_runtime_created": False,
            "telemetry_enabled": False,
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(install.subprocess, "run", return_value=completed) as command:
            self.assertEqual(
                install.verify_computer_use_runtime(self.source, self.runtime),
                (True, "0.28.2"),
            )

        args, kwargs = command.call_args
        self.assertEqual(
            args[0],
            [str(self.python), "-I", "-X", "utf8", str(self.backend)],
        )
        self.assertEqual(Path(args[0][-1]).name, "backend.py")
        self.assertEqual(kwargs["timeout"], 90)
        self.assertEqual(kwargs["env"]["PYTHONPATH"], "")
        self.assertEqual(kwargs["env"]["CUA_DRIVER_RS_TELEMETRY_ENABLED"], "0")

    def test_missing_or_unsafe_verification_flags_are_rejected(self):
        for field in ("native_runtime_created", "telemetry_enabled"):
            for unsafe in (None, True, 0, "false"):
                payload = {"ok": True, "cua_driver": "0.28.2", "native_runtime_created": False, "telemetry_enabled": False}
                if unsafe is None:
                    payload.pop(field)
                else:
                    payload[field] = unsafe
                completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with self.subTest(field=field, unsafe=unsafe), mock.patch.object(install.subprocess, "run", return_value=completed):
                    self.assertFalse(install.verify_computer_use_runtime(self.source, self.runtime)[0])

    def test_non_object_json_is_rejected(self):
        completed = subprocess.CompletedProcess([], 0, "[]", "")
        with mock.patch.object(install.subprocess, "run", return_value=completed):
            self.assertFalse(install.verify_computer_use_runtime(self.source, self.runtime)[0])

    def test_invalid_json_is_rejected(self):
        completed = subprocess.CompletedProcess([], 1, "not-json", "")
        with mock.patch.object(install.subprocess, "run", return_value=completed):
            valid, detail = install.verify_computer_use_runtime(self.source, self.runtime)

        self.assertFalse(valid)
        self.assertIn("未返回有效 JSON", detail)

    def test_missing_interpreter_fails_without_starting_process(self):
        self.python.unlink()
        with mock.patch.object(install.subprocess, "run") as command:
            self.assertEqual(
                install.verify_computer_use_runtime(self.source, self.runtime),
                (False, "缺少 Python 解释器"),
            )
        command.assert_not_called()


class DeployComputerUseFixture(unittest.TestCase):
    """运行时和技能目标都限制在 TemporaryDirectory。"""

    def enterContext(self, manager):
        return self.contexts.enter_context(manager)

    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.source = self.repo / "skills" / install.COMPUTER_USE_SKILL
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "SKILL.md").write_text("# computer-use\n", encoding="utf-8")
        (self.source / "scripts" / "backend.py").write_text("# 校验入口\n", encoding="utf-8")
        (self.source / "scripts" / "requirements.lock").write_text(
            "cua-driver==0.28.2 --hash=sha256:abc\n", encoding="utf-8"
        )
        self.skills_dir = self.root / "deployed-skills"
        self.runtime = self.root / "runtime-home" / "runtime"
        self.output = self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))
        self.enterContext(mock.patch.object(install, "REPO_ROOT", self.repo))
        self.enterContext(mock.patch.object(install.sys, "platform", "win32"))
        self.enterContext(mock.patch.dict(
            install.os.environ,
            {
                install.COMPUTER_USE_RUNTIME_ENV: str(self.runtime),
                "PI_OFFLINE": "",
            },
            clear=False,
        ))
        self.verify = self.enterContext(mock.patch.object(install, "verify_computer_use_runtime"))
        self.uv = self.enterContext(mock.patch.object(
            install, "uv_executable", return_value="test-uv"
        ))
        self.runner = self.enterContext(mock.patch.object(install, "run"))
        self.deploy = self.enterContext(mock.patch.object(install, "deploy_computer_use_skill"))
        self.deploy.return_value = None
        self.subprocess = self.enterContext(mock.patch.object(
            install.subprocess,
            "run",
            side_effect=AssertionError("不应执行真实子进程"),
        ))

    def seed_old_runtime(self):
        python = install.computer_use_runtime_python(self.runtime)
        python.parent.mkdir(parents=True)
        python.write_bytes(b"old fake python")
        (self.runtime / "old.txt").write_text("keep old runtime\n", encoding="utf-8")

    def fake_uv(self, args, cwd=None):
        del cwd
        if args[:2] == ["test-uv", "venv"]:
            staged = Path(args[-1])
            python = install.computer_use_runtime_python(staged)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"new fake python")
            (staged / "new.txt").write_text("new runtime\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["test-uv", "pip", "install"]:
            return subprocess.CompletedProcess(args, 0)
        self.fail(f"Unexpected command: {args}")


class DeployComputerUseOfflineTest(DeployComputerUseFixture):
    """PI_OFFLINE 只能复用已验证运行时，绝不尝试创建环境。"""

    def test_valid_existing_runtime_is_deployed_offline(self):
        self.seed_old_runtime()
        self.verify.return_value = (True, "0.5.0")
        with mock.patch.dict(install.os.environ, {"PI_OFFLINE": "1"}):
            install.deploy_computer_use(self.skills_dir)

        self.verify.assert_called_once_with(self.source, self.runtime)
        self.deploy.assert_called_once_with(
            self.source,
            self.skills_dir / install.COMPUTER_USE_SKILL,
        )
        self.uv.assert_not_called()
        self.runner.assert_not_called()
        self.subprocess.assert_not_called()

    def test_missing_runtime_is_skipped_offline(self):
        self.verify.return_value = (False, "缺少 Python 解释器")
        with mock.patch.dict(install.os.environ, {"PI_OFFLINE": "true"}):
            install.deploy_computer_use(self.skills_dir)

        self.deploy.assert_not_called()
        self.uv.assert_not_called()
        self.runner.assert_not_called()
        self.assertIn("现有运行时不可用", self.output.getvalue())


class DeployComputerUseOnlineTest(DeployComputerUseFixture):
    """在线分支仅模拟 uv，并验证 staging、切换和回滚语义。"""

    def test_staged_venv_is_hash_installed_verified_switched_and_deployed(self):
        self.seed_old_runtime()
        self.runner.side_effect = self.fake_uv
        self.verify.side_effect = [
            (True, "old-version"),
            (True, "0.5.0"),
            (True, "0.5.0"),
        ]

        install.deploy_computer_use(self.skills_dir)

        self.assertFalse((self.runtime / "old.txt").exists())
        self.assertEqual((self.runtime / "new.txt").read_text(encoding="utf-8"), "new runtime\n")
        self.assertFalse(self.runtime.with_name("runtime.previous").exists())
        self.assertEqual(self.runner.call_count, 2)
        venv_args = self.runner.call_args_list[0].args[0]
        staged = Path(venv_args[-1])
        self.assertEqual(
            venv_args,
            ["test-uv", "venv", "--python", install.COMPUTER_USE_PYTHON, str(staged)],
        )
        pip_args = self.runner.call_args_list[1].args[0]
        self.assertEqual(
            pip_args,
            [
                "test-uv", "pip", "install", "--python",
                str(install.computer_use_runtime_python(staged)),
                "--require-hashes", "--requirement",
                str(self.source / "scripts" / "requirements.lock"),
            ],
        )
        self.assertEqual(
            self.verify.call_args_list,
            [
                mock.call(self.source, self.runtime),
                mock.call(self.source, staged),
                mock.call(self.source, self.runtime),
            ],
        )
        self.deploy.assert_called_once_with(
            self.source,
            self.skills_dir / install.COMPUTER_USE_SKILL,
            keep_backup=True,
        )
        self.subprocess.assert_not_called()

    def test_failed_staged_validation_preserves_old_runtime(self):
        self.seed_old_runtime()
        self.runner.side_effect = self.fake_uv
        self.verify.side_effect = [
            (True, "old-version"),
            (False, "incompatible_version"),
        ]

        install.deploy_computer_use(self.skills_dir)

        self.assertEqual(
            (self.runtime / "old.txt").read_text(encoding="utf-8"),
            "keep old runtime\n",
        )
        self.assertFalse((self.runtime / "new.txt").exists())
        self.assertFalse(self.runtime.with_name("runtime.previous").exists())
        staged = Path(self.runner.call_args_list[0].args[0][-1])
        self.assertFalse(staged.exists())
        self.deploy.assert_not_called()
        self.assertIn("新运行时兼容性校验失败", self.output.getvalue())
        self.subprocess.assert_not_called()

    def test_failed_post_move_validation_restores_old_runtime(self):
        self.seed_old_runtime()
        self.runner.side_effect = self.fake_uv
        self.verify.side_effect = [
            (True, "old-version"),
            (True, "0.8.5"),
            (False, "moved-runtime-invalid"),
        ]

        install.deploy_computer_use(self.skills_dir)

        self.assertEqual((self.runtime / "old.txt").read_text(encoding="utf-8"),
                         "keep old runtime\n")
        self.assertFalse((self.runtime / "new.txt").exists())
        self.assertFalse(self.runtime.with_name("runtime.previous").exists())
        self.deploy.assert_not_called()
        self.assertIn("切换后校验失败", self.output.getvalue())

    def test_interrupted_switch_restores_previous_before_verification(self):
        backup = self.runtime.with_name("runtime.previous")
        python = install.computer_use_runtime_python(backup)
        python.parent.mkdir(parents=True)
        python.write_bytes(b"recoverable python")
        (backup / "old.txt").write_text("recover me\n", encoding="utf-8")
        self.runner.side_effect = self.fake_uv
        self.verify.side_effect = [(True, "old-version"), (False, "stop")]

        install.deploy_computer_use(self.skills_dir)

        self.assertTrue((self.runtime / "old.txt").is_file())
        self.assertFalse(backup.exists())


class ComputerUseTransactionRecoveryTest(unittest.TestCase):
    """中断发生在两个目录切换之间时恢复为同一旧版本。"""

    def test_uncommitted_transaction_restores_runtime_and_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime_backup = root / "runtime.previous"
            skill = root / "skills" / "computer-use"
            skill_backup = skill.with_name("computer-use.previous")
            for directory, marker in (
                (runtime, "new runtime"), (runtime_backup, "old runtime"),
                (skill, "new skill"), (skill_backup, "old skill"),
            ):
                directory.mkdir(parents=True)
                (directory / "version.txt").write_text(marker, encoding="utf-8")
            install._write_computer_use_transaction(runtime, {
                "phase": "skill_switched",
                "runtime": str(runtime),
                "skill": str(skill),
                "runtime_had_previous": True,
                "skill_had_previous": True,
            })

            install.recover_computer_use_transaction(runtime, skill)

            self.assertEqual((runtime / "version.txt").read_text(encoding="utf-8"), "old runtime")
            self.assertEqual((skill / "version.txt").read_text(encoding="utf-8"), "old skill")
            self.assertFalse(runtime_backup.exists())
            self.assertFalse(skill_backup.exists())
            self.assertFalse(install._computer_use_transaction_path(runtime).exists())


class ComputerUseAtomicSkillDeployTest(unittest.TestCase):
    """技能目录先完整 staging，再替换已有版本。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "SKILL.md").write_text("new skill\n", encoding="utf-8")
        (self.source / "scripts" / "computer.py").write_text("new entry\n", encoding="utf-8")
        (self.source / "scripts" / "compatibility.json").write_text("{}\n", encoding="utf-8")
        self.destination = self.root / "skills" / "computer-use"
        self.destination.mkdir(parents=True)
        (self.destination / "SKILL.md").write_text("old skill\n", encoding="utf-8")
        (self.destination / "stale.txt").write_text("stale\n", encoding="utf-8")

    def test_success_replaces_complete_directory_without_stale_files(self):
        install.deploy_computer_use_skill(self.source, self.destination)
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "new skill\n")
        self.assertFalse((self.destination / "stale.txt").exists())
        self.assertFalse(self.destination.with_name("computer-use.previous").exists())

    def test_failed_staging_keeps_existing_deployment(self):
        with mock.patch.object(install, "deploy_generic", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                install.deploy_computer_use_skill(self.source, self.destination)
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "old skill\n")

    def test_backup_cleanup_failure_keeps_new_skill_without_raising(self):
        backup = self.destination.with_name("computer-use.previous")
        real_rmtree = install.shutil.rmtree

        def remove(path, *args, **kwargs):
            if Path(path) == backup:
                raise OSError("backup busy")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(install.shutil, "rmtree", side_effect=remove):
            install.deploy_computer_use_skill(self.source, self.destination)
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "new skill\n")
        self.assertTrue(backup.exists())


class ComputerUseMainRoutingTest(unittest.TestCase):
    """CLI 名称在工具部署与普通技能复制之间保持明确边界。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills_dir = self.root / "skills"
        self.pi_agent_dir = self.root / "pi-agent"

    def test_tools_name_routes_only_computer_use_without_node_prerequisites(self):
        argv = [
            "install.py", "--tools", "--name", "computer-use",
            "--skills-dir", str(self.skills_dir),
            "--pi-agent-dir", str(self.pi_agent_dir),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(install, "check_python_prerequisite") as python_check,
            mock.patch.object(install, "check_prerequisites") as node_check,
            mock.patch.object(install, "deploy_browser_use") as browser,
            mock.patch.object(install, "deploy_computer_use") as computer,
            mock.patch.object(install, "deploy_context") as context,
            mock.patch.object(install, "configure_shell") as shell,
            mock.patch.object(install, "deploy_binaries") as binaries,
            mock.patch.object(install, "deploy_extensions") as extensions,
            mock.patch.object(install, "deploy_skills") as skills,
        ):
            install.main()

        python_check.assert_called_once_with()
        node_check.assert_not_called()
        computer.assert_called_once_with(self.skills_dir)
        browser.assert_not_called()
        for other in (context, shell, binaries, extensions, skills):
            other.assert_not_called()

    def test_default_install_does_not_redeploy_tool_skills_generically(self):
        argv = [
            "install.py", "--skills-dir", str(self.skills_dir),
            "--pi-agent-dir", str(self.pi_agent_dir),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(install, "check_python_prerequisite"),
            mock.patch.object(install, "check_prerequisites"),
            mock.patch.object(install, "deploy_context"),
            mock.patch.object(install, "configure_shell"),
            mock.patch.object(install, "deploy_binaries"),
            mock.patch.object(install, "deploy_extensions"),
            mock.patch.object(install, "deploy_tools") as tools,
            mock.patch.object(install, "deploy_skills") as skills,
        ):
            install.main()

        tools.assert_called_once_with(self.skills_dir, None)
        skills.assert_called_once_with(
            install.REPO_ROOT / "skills", self.skills_dir, None, False,
            skip_names={install.BROWSER_USE_SKILL, install.COMPUTER_USE_SKILL},
        )

    def test_combined_tools_and_skills_skip_tool_skills_in_generic_pass(self):
        argv = [
            "install.py", "--tools", "--skills", "--force",
            "--skills-dir", str(self.skills_dir),
            "--pi-agent-dir", str(self.pi_agent_dir),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(install, "check_python_prerequisite"),
            mock.patch.object(install, "check_prerequisites"),
            mock.patch.object(install, "deploy_tools") as tools,
            mock.patch.object(install, "deploy_skills") as skills,
        ):
            install.main()

        tools.assert_called_once_with(self.skills_dir, None)
        skills.assert_called_once_with(
            install.REPO_ROOT / "skills", self.skills_dir, None, True,
            skip_names={install.BROWSER_USE_SKILL, install.COMPUTER_USE_SKILL},
        )

    def test_unknown_tools_name_is_parser_error(self):
        stderr = io.StringIO()
        argv = ["install.py", "--tools", "--name", "unknown-tool"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("sys.stderr", stderr),
            mock.patch.object(install, "check_python_prerequisite") as python_check,
            mock.patch.object(install, "deploy_tools") as tools,
        ):
            with self.assertRaises(SystemExit) as raised:
                install.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--tools 不支持技能：unknown-tool", stderr.getvalue())
        python_check.assert_not_called()
        tools.assert_not_called()

    def test_name_alone_uses_only_normal_skill_copy(self):
        argv = [
            "install.py", "--name", "computer-use",
            "--skills-dir", str(self.skills_dir),
            "--pi-agent-dir", str(self.pi_agent_dir),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(install, "check_python_prerequisite") as python_check,
            mock.patch.object(install, "check_prerequisites") as node_check,
            mock.patch.object(install, "deploy_tools") as tools,
            mock.patch.object(install, "deploy_skills") as skills,
            mock.patch.object(install, "deploy_context") as context,
            mock.patch.object(install, "configure_shell") as shell,
            mock.patch.object(install, "deploy_binaries") as binaries,
            mock.patch.object(install, "deploy_extensions") as extensions,
        ):
            install.main()

        python_check.assert_called_once_with()
        node_check.assert_called_once_with()
        tools.assert_not_called()
        skills.assert_called_once_with(
            install.REPO_ROOT / "skills",
            self.skills_dir,
            "computer-use",
            False,
        )
        for other in (context, shell, binaries, extensions):
            other.assert_not_called()


if __name__ == "__main__":
    unittest.main()
