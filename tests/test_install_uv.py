#!/usr/bin/env python3
"""install.py uv 自动安装逻辑的静态测试（不联网、不执行安装脚本）。

运行：python -m unittest discover -s tests -p 'test_*.py'
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install  # noqa: E402


def reset_uv_cache():
    """重置 ensure_uv 的进程内缓存，避免用例间串扰。"""
    install._uv_checked = False
    install._uv_resolved = None


class EnsureUvTest(unittest.TestCase):
    """ensure_uv：已有 uv 直接返回；缺失时自动安装一次并缓存结果。"""

    def setUp(self):
        reset_uv_cache()

    def tearDown(self):
        reset_uv_cache()

    def test_returns_existing_uv_without_installing(self):
        with mock.patch.object(install, "uv_executable", return_value="C:/bin/uv.exe") as uv_exec, \
             mock.patch.object(install, "install_uv") as installer:
            self.assertEqual(install.ensure_uv(), "C:/bin/uv.exe")
        installer.assert_not_called()
        uv_exec.assert_called_once()

    def test_installs_once_and_caches(self):
        with mock.patch.object(install, "uv_executable", return_value=None), \
             mock.patch.object(install, "install_uv", return_value="/home/u/.local/bin/uv") as installer:
            self.assertEqual(install.ensure_uv(), "/home/u/.local/bin/uv")
            self.assertEqual(install.ensure_uv(), "/home/u/.local/bin/uv")
        installer.assert_called_once()

    def test_failed_install_is_not_retried(self):
        with mock.patch.object(install, "uv_executable", return_value=None), \
             mock.patch.object(install, "install_uv", return_value=None) as installer:
            self.assertIsNone(install.ensure_uv())
            self.assertIsNone(install.ensure_uv())
        installer.assert_called_once()


class InstallUvTest(unittest.TestCase):
    """install_uv：离线/无下载器跳过；成功后从默认落点返回 uv 路径。"""

    def test_offline_mode_skips_download(self):
        with mock.patch.dict("os.environ", {"PI_OFFLINE": "1"}), \
             mock.patch("subprocess.run") as runner, \
             mock.patch("builtins.print") as output:
            self.assertIsNone(install.install_uv())
        runner.assert_not_called()
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("PI_OFFLINE", text)

    def test_windows_uses_powershell_official_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp) / "uv.exe"
            location.write_bytes(b"")
            with mock.patch.object(sys, "platform", "win32"), \
                 mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as runner, \
                 mock.patch.object(install, "uv_fallback_locations", return_value=[location]):
                self.assertEqual(install.install_uv(), str(location))
        command = runner.call_args.args[0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("install.ps1", command[-1])
        self.assertEqual(runner.call_args.kwargs["timeout"], install.UV_INSTALL_TIMEOUT)

    def test_posix_prefers_curl(self):
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp) / "uv"
            location.write_bytes(b"")
            with mock.patch.object(sys, "platform", "linux"), \
                 mock.patch("shutil.which", return_value="/usr/bin/curl"), \
                 mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as runner, \
                 mock.patch.object(install, "uv_fallback_locations", return_value=[location]):
                self.assertEqual(install.install_uv(), str(location))
        command = runner.call_args.args[0]
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertIn("install.sh", command[2])

    def test_posix_without_downloader_skips(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch("subprocess.run") as runner, \
             mock.patch("builtins.print") as output:
            self.assertIsNone(install.install_uv())
        runner.assert_not_called()
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("powershell/curl/wget", text)

    def test_installer_failure_returns_none(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)), \
             mock.patch.object(install, "uv_fallback_locations") as locations:
            self.assertIsNone(install.install_uv())
        locations.assert_not_called()

    def test_github_proxy_is_passed_to_installer(self):
        location = Path("C:/Users/u/.local/bin/uv.exe")
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict("os.environ", {"GITHUB_PROXY": "127.0.0.1:7897"}), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as runner, \
             mock.patch.object(install, "uv_fallback_locations", return_value=[location]):
            install.install_uv()
        child_env = runner.call_args.kwargs["env"]
        self.assertEqual(child_env["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(child_env["HTTPS_PROXY"], "http://127.0.0.1:7897")


class UvFallbackLocationsTest(unittest.TestCase):
    """uv_fallback_locations：按平台补 .exe 后缀，覆盖新旧默认落点。"""

    def test_windows_locations(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(Path, "home", return_value=Path("C:/Users/u")):
            self.assertEqual(
                install.uv_fallback_locations(),
                [Path("C:/Users/u/.local/bin/uv.exe"), Path("C:/Users/u/.cargo/bin/uv.exe")],
            )

    def test_posix_locations(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(Path, "home", return_value=Path("/home/u")):
            self.assertEqual(
                install.uv_fallback_locations(),
                [Path("/home/u/.local/bin/uv"), Path("/home/u/.cargo/bin/uv")],
            )


class RepairUvToolEnvTest(unittest.TestCase):
    """半成品工具环境（缺 pyvenv.cfg）清理：只清确认损坏的，不动健康环境。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tools = Path(tmp.name) / "tools"
        self.tool_env = self.tools / "browser-harness"
        self.tool_env.mkdir(parents=True)
        # 模拟 `uv tool dir` 查询，返回本用例的临时 tools 目录
        self.uv_tool_dir = mock.patch.object(
            install.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout=f"{self.tools}\n"),
        )

    def seed_interpreter(self):
        # 同时建 Windows/Linux 两种布局，用例本身与平台无关
        scripts = self.tool_env / "Scripts"
        scripts.mkdir()
        (scripts / "python.exe").write_bytes(b"")
        (self.tool_env / "bin").mkdir()
        (self.tool_env / "bin" / "python").write_bytes(b"")

    def test_removes_broken_env(self):
        self.seed_interpreter()
        with self.uv_tool_dir:
            self.assertTrue(install.repair_uv_tool_env("uv", "browser-harness"))
        self.assertFalse(self.tool_env.exists())

    def test_keeps_healthy_env(self):
        self.seed_interpreter()
        (self.tool_env / "pyvenv.cfg").write_text("", encoding="utf-8")
        with self.uv_tool_dir:
            self.assertFalse(install.repair_uv_tool_env("uv", "browser-harness"))
        self.assertTrue(self.tool_env.exists())

    def test_ignores_absent_env(self):
        with self.uv_tool_dir:
            self.assertFalse(install.repair_uv_tool_env("uv", "browser-harness"))

    def test_tool_dir_query_failure_is_ignored(self):
        with mock.patch.object(
            install.subprocess, "run",
            return_value=mock.Mock(returncode=1, stdout=""),
        ):
            self.assertFalse(install.repair_uv_tool_env("uv", "browser-harness"))


if __name__ == "__main__":
    unittest.main()
