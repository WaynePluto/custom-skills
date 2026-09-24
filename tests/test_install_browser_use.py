#!/usr/bin/env python3
"""install.py browser-use 技能及 browser-harness 依赖的静态测试（不联网、不启动浏览器）。

运行：python -m unittest discover -s tests -p 'test_*.py'
"""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install  # noqa: E402


class ParseUvToolListTest(unittest.TestCase):
    """uv tool list 解析：包名取非缩进行首列，版本要求 v 前缀。"""

    def test_normal_output(self):
        output = (
            "browser-harness v0.1.13\n"
            "- browser-harness\n"
            "- browser-harness-mcp\n"
            "fd v10.2.0\n"
            "- fd\n"
        )
        self.assertEqual(
            install.parse_uv_tool_list(output),
            {"browser-harness": "0.1.13", "fd": "10.2.0"},
        )

    def test_empty_output(self):
        self.assertEqual(install.parse_uv_tool_list(""), {})
        self.assertEqual(install.parse_uv_tool_list("No tools installed\n"), {})

    def test_ignores_lines_without_version(self):
        self.assertEqual(install.parse_uv_tool_list("some-tool\n"), {})


class ApplySkillOverridesTest(unittest.TestCase):
    """frontmatter 覆盖：同名 key 整行替换、缺失 key 插入、正文不动。"""

    SAMPLE = (
        "---\n"
        'name: "browser-harness"\n'
        'description: "Always use browser-harness for any web interaction."\n'
        "license: MIT\n"
        "---\n"
        "\n"
        "# body\n"
        "\n"
        "name: not-frontmatter\n"
    )

    def test_replaces_and_preserves(self):
        result = install.apply_skill_overrides(
            self.SAMPLE,
            {"name": "browser-use", "description": "何时使用；何时不使用。"},
        )
        self.assertIn('name: "browser-use"', result)
        self.assertNotIn('name: "browser-harness"', result)
        self.assertIn('description: "何时使用；何时不使用。"', result)
        self.assertIn("license: MIT", result)  # 未覆盖的 key 保留
        self.assertIn("name: not-frontmatter", result)  # 正文不动
        self.assertNotIn("Always use", result)

    def test_inserts_missing_keys(self):
        result = install.apply_skill_overrides(
            "---\nlicense: MIT\n---\n\nbody\n",
            {"name": "browser-use"},
        )
        self.assertIn('name: "browser-use"', result)
        self.assertIn("license: MIT", result)

    def test_prepends_frontmatter_when_absent(self):
        result = install.apply_skill_overrides("just body\n", {"name": "x"})
        self.assertTrue(result.startswith('---\nname: "x"\n---\n'))
        self.assertIn("just body", result)

    def test_returns_text_when_frontmatter_unclosed(self):
        text = "---\nname: a\n"
        self.assertEqual(install.apply_skill_overrides(text, {"name": "b"}), text)

    def test_escapes_quotes_and_backslashes(self):
        result = install.apply_skill_overrides("body", {"description": '带"引号"和\\反斜杠'})
        self.assertIn('description: "带\\"引号\\"和\\\\反斜杠"', result)


class BrowserHarnessBinTest(unittest.TestCase):
    """可执行文件定位：PATH 命中优先，否则回落 uv bin / 默认目录。"""

    def test_path_hit_wins(self):
        with mock.patch("shutil.which", return_value="C:/tools/browser-harness.exe"):
            self.assertEqual(
                install.browser_harness_bin("uv"),
                Path("C:/tools/browser-harness.exe"),
            )

    def test_default_location_fallback(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")), \
             mock.patch.object(Path, "home", return_value=Path("/home/u")):
            # 目录不存在 → None，不抛异常
            self.assertIsNone(install.browser_harness_bin("uv"))


class BrowserHarnessPathWarningTest(unittest.TestCase):
    """PATH 检查：可直接调用时静默，否则提示目录和重启步骤。"""

    def test_no_warning_when_command_is_on_path(self):
        with mock.patch("shutil.which", return_value="C:/tools/browser-harness.exe"), \
             mock.patch("builtins.print") as output:
            install.warn_if_browser_harness_not_on_path(Path("C:/tools/browser-harness.exe"))
        output.assert_not_called()

    def test_windows_warning_contains_bin_dir_and_restart_step(self):
        harness = Path("C:/Users/test/.local/bin/browser-harness.exe")
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch("builtins.print") as output:
            install.warn_if_browser_harness_not_on_path(harness)

        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn(str(harness.parent), text)
        self.assertIn("用户 Path", text)
        self.assertIn("重启 VS Code", text)


REAL_BROWSER_USE_SKILL = Path(__file__).resolve().parent.parent / "skills" / "browser-use"
UPSTREAM_SKILL = (
    "---\n"
    "name: browser-harness\n"
    "description: Always use the bare CLI.\n"
    "---\n\n"
    "# UPSTREAM_ONLY_WORKFLOW\n\n"
    "Run `browser-harness` directly; do not use the local entry.\n"
)


class BrowserUseFixture(unittest.TestCase):
    """所有写入限定在临时目录，模板及审定元数据取自真实技能。"""

    def enterContext(self, manager):
        # 保持测试与项目声明的 Python 3.10 兼容。
        return self.contexts.enter_context(manager)

    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.source = self.repo / "skills" / "browser-use"
        self.source.mkdir(parents=True)
        self.skills_dir = self.root / "deployed-skills"
        self.destination = self.skills_dir / "browser-use"
        for relative in (
            "skill-template.md", "skill-overrides.json", "scripts/compatibility.json",
        ):
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REAL_BROWSER_USE_SKILL / relative, target)
        self.template = (self.source / "skill-template.md").read_text(encoding="utf-8")
        self.overrides = json.loads(
            (self.source / "skill-overrides.json").read_text(encoding="utf-8")
        )
        self.versions = json.loads(
            (self.source / "scripts" / "compatibility.json").read_text(encoding="utf-8")
        )["browser_harness_versions"]
        self.harness = self.root / "bin" / "browser-harness"
        self.upstream = UPSTREAM_SKILL
        self.script = "print('new Chrome entry')\n"
        (self.source / "scripts" / "chrome.py").write_text(self.script, encoding="utf-8")
        (self.source / "scripts" / "chrome_runtime.py").write_text("new runtime\n", encoding="utf-8")
        (self.source / "references").mkdir()
        (self.source / "references" / "local-entry.md").write_text("本地参考\n", encoding="utf-8")
        (self.source / "NOTES.md").write_text("# 本机 Chrome 入口\n", encoding="utf-8")
        self.output = self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))
        self.command = self.enterContext(mock.patch.object(
            install.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")
        ))
        self.run = self.enterContext(mock.patch.object(
            install, "run", side_effect=AssertionError("Unexpected installation")
        ))

    def seed_existing_deployment(self):
        (self.destination / "scripts").mkdir(parents=True)
        (self.destination / "SKILL.md").write_text("old deployed skill\n", encoding="utf-8")
        (self.destination / "scripts" / "chrome.py").write_text("old script\n", encoding="utf-8")
        (self.destination / "obsolete.txt").write_text("stale\n", encoding="utf-8")

        (self.destination / "references").mkdir()
        for relative in (
            "scripts/chrome_runtime.py", "scripts/obsolete.py", "references/upstream-skill.md",
            "references/local-entry.md", "references/obsolete.md",
        ):
            (self.destination / relative).write_bytes(b"stale\r\n")

    def snapshot_files(self, directory):
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }

    def assert_only_browser_use_deployed(self):
        self.assertEqual(sorted(path.name for path in self.skills_dir.iterdir()), ["browser-use"])

    def assert_local_skill(self, directory):
        self.assertEqual(self.overrides["name"], "browser-use")
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter, body = text[4:].split("\n---\n", 1)
        for key, value in self.overrides.items():
            self.assertIn(f"{key}: {json.dumps(value, ensure_ascii=False)}", frontmatter)
        self.assertEqual(body, self.template)
        self.assertIn("scripts/chrome.py", body)
        self.assertNotIn("UPSTREAM_ONLY_WORKFLOW", text)
        self.assertEqual(
            (directory / "references" / "upstream-skill.md").read_text(encoding="utf-8"),
            self.upstream,
        )


class GenerateBrowserUseSkillTest(BrowserUseFixture):
    """默认正文来自本地入口，上游说明只能成为参考文件。"""

    def test_generates_local_template_with_reviewed_overrides_and_raw_reference(self):
        skill = install.generate_browser_use_skill(self.source, self.upstream)
        self.assertEqual(skill, self.source / "SKILL.md")
        self.assert_local_skill(self.source)
        self.assertNotIn(b"\r", skill.read_bytes())
        self.assertNotIn(b"\r", (self.source / "references/upstream-skill.md").read_bytes())
        self.command.assert_not_called()
        self.run.assert_not_called()

    def test_repeated_generation_is_idempotent_and_upstream_changes_only_reference(self):
        skill = install.generate_browser_use_skill(self.source, self.upstream)
        reference = self.source / "references" / "upstream-skill.md"
        original = (skill.read_bytes(), reference.read_bytes())
        self.assertEqual(install.generate_browser_use_skill(self.source, self.upstream), skill)
        self.assertEqual((skill.read_bytes(), reference.read_bytes()), original)

        self.upstream += "\nNew upstream helpers and cloud workflow.\n"
        install.generate_browser_use_skill(self.source, self.upstream)
        self.assertEqual(skill.read_bytes(), original[0])
        self.assertNotEqual(reference.read_bytes(), original[1])
        self.assert_local_skill(self.source)

    def test_existing_bare_cli_skill_is_replaced_even_when_upstream_is_unchanged(self):
        (self.source / "SKILL.md").write_text(self.upstream, encoding="utf-8")
        install.generate_browser_use_skill(self.source, self.upstream)
        self.assert_local_skill(self.source)


class DeployBrowserUseToolsTest(BrowserUseFixture):
    """模拟 uv/CLI；真实生成和目录复制仅作用于临时目录。"""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(install, "REPO_ROOT", self.repo))
        self.uv = self.enterContext(mock.patch.object(
            install, "uv_executable", return_value="test-uv"
        ))
        self.find_harness = self.enterContext(mock.patch.object(
            install, "browser_harness_bin", return_value=self.harness
        ))
        self.enterContext(mock.patch.object(install, "warn_if_browser_harness_not_on_path"))
        self.enterContext(mock.patch.dict(install.os.environ, {"PI_OFFLINE": "0"}))
        self.installed = True
        self.command.side_effect = self.fake_cli
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0)
        self.deploy = self.enterContext(mock.patch.object(
            install, "deploy_generic", wraps=install.deploy_generic
        ))

    def fake_cli(self, args, **kwargs):
        if args == ["test-uv", "tool", "list"]:
            output = f"browser-harness v{self.versions[0]}\n- browser-harness\n"
            return subprocess.CompletedProcess(args, 0, output if self.installed else "")
        if args == [str(self.harness), "skill"]:
            return subprocess.CompletedProcess(args, 0, self.upstream)
        if args == [str(self.harness), "--version"]:
            return subprocess.CompletedProcess(args, 0, self.versions[0] + "\n")
        self.fail(f"Unexpected command: {args}")

    def assert_full_deployment(self):
        self.deploy.assert_called_once_with(self.source, self.destination, force=True)
        self.assert_local_skill(self.source)
        self.assert_local_skill(self.destination)
        self.assertEqual(
            (self.destination / "scripts" / "chrome.py").read_text(encoding="utf-8"), self.script
        )
        self.assert_only_browser_use_deployed()
        for relative in ("obsolete.txt", "scripts/obsolete.py", "references/obsolete.md"):
            self.assertFalse((self.destination / relative).exists())
        for relative in (
            "scripts/compatibility.json", "scripts/chrome_runtime.py", "references/local-entry.md",
            "NOTES.md",
        ):
            self.assertEqual(
                (self.destination / relative).read_bytes(), (self.source / relative).read_bytes()
            )
        for relative in ("skill-template.md", "skill-overrides.json"):
            self.assertFalse((self.destination / relative).exists())
        self.command.assert_any_call(
            [str(self.harness), "skill"], capture_output=True, text=True, encoding="utf-8", timeout=120
        )

    def assert_metadata_rejected(self):
        self.seed_existing_deployment()
        (self.source / "SKILL.md").write_text("safe local entry\n", encoding="utf-8")
        reference = self.source / "references" / "upstream-skill.md"
        reference.parent.mkdir(exist_ok=True)
        reference.write_text("previous upstream reference\n", encoding="utf-8")
        with mock.patch.object(install, "deploy_generic") as deploy:
            install.deploy_browser_use(self.skills_dir)
        deploy.assert_not_called()
        self.assertEqual(
            (self.source / "SKILL.md").read_text(encoding="utf-8"), "safe local entry\n"
        )
        self.assertEqual(reference.read_text(encoding="utf-8"), "previous upstream reference\n")
        self.assertEqual(
            (self.destination / "SKILL.md").read_text(encoding="utf-8"), "old deployed skill\n"
        )
        self.assertEqual(
            (self.destination / "scripts" / "chrome.py").read_text(encoding="utf-8"), "old script\n"
        )
        self.assertIn("不部署上游裸 CLI", self.output.getvalue())

    def run_tools_main(self):
        with (
            mock.patch.object(sys, "argv", [
                "install.py", "--tools", "--name", "browser-use",
                "--skills-dir", str(self.skills_dir),
                "--pi-agent-dir", str(self.root / "pi-agent"),
            ]),
            mock.patch.object(install, "check_prerequisites") as prerequisites,
            mock.patch.object(install, "deploy_skills") as skills,
            mock.patch.object(install, "deploy_context") as context,
            mock.patch.object(install, "configure_shell") as shell,
            mock.patch.object(install, "deploy_extensions") as extensions,
            mock.patch.object(install, "deploy_binaries") as binaries,
        ):
            install.main()
        prerequisites.assert_called_once_with()
        for other_deployment in (skills, context, shell, extensions, binaries):
            other_deployment.assert_not_called()

    def test_tools_main_forces_full_deployment_not_only_skill_copy(self):
        self.seed_existing_deployment()
        self.run_tools_main()
        self.run.assert_called_once_with(["test-uv", "tool", "upgrade", "browser-harness"])
        self.assert_full_deployment()

    def test_tools_installs_browser_harness_package_but_deploys_browser_use_skill(self):
        self.installed = False
        self.run_tools_main()
        self.run.assert_called_once_with([
            "test-uv", "tool", "install", "--python", "3.12", "browser-harness",
        ])
        self.assertEqual(install.BROWSER_HARNESS, "browser-harness")
        self.assertEqual(install.BROWSER_USE_SKILL, "browser-use")
        self.assert_full_deployment()

    def test_offline_installed_still_generates_and_fully_deploys(self):
        self.seed_existing_deployment()
        with mock.patch.dict(install.os.environ, {"PI_OFFLINE": "1"}):
            install.deploy_browser_use(self.skills_dir)
        self.run.assert_not_called()
        self.assert_full_deployment()
        self.assertIn("跳过升级检查", self.output.getvalue())

    def test_offline_uninstalled_skips_without_installing_or_deploying(self):
        self.installed = False
        with mock.patch.dict(install.os.environ, {"PI_OFFLINE": "1"}):
            install.deploy_browser_use(self.skills_dir)
        self.run.assert_not_called()
        self.find_harness.assert_not_called()
        self.deploy.assert_not_called()
        self.assertFalse((self.source / "SKILL.md").exists())
        self.assertFalse(self.destination.exists())
        self.assertIn("尚未安装", self.output.getvalue())

    def test_no_uv_skips_without_touching_existing_deployment(self):
        self.seed_existing_deployment()
        self.uv.return_value = None
        install.deploy_browser_use(self.skills_dir)
        self.command.assert_not_called()
        self.run.assert_not_called()
        self.find_harness.assert_not_called()
        self.deploy.assert_not_called()
        self.assertFalse((self.source / "SKILL.md").exists())
        self.assertEqual(
            (self.destination / "scripts" / "chrome.py").read_text(encoding="utf-8"), "old script\n"
        )
        self.assertIn("未找到 uv", self.output.getvalue())

    def test_failed_upgrade_still_generates_from_installed_cli(self):
        self.seed_existing_deployment()
        self.run.return_value = subprocess.CompletedProcess([], 1)
        install.deploy_browser_use(self.skills_dir)
        self.assert_full_deployment()
        self.assertIn("升级失败", self.output.getvalue())

    def test_missing_template_does_not_deploy_bare_cli(self):
        (self.source / "skill-template.md").unlink()
        self.assert_metadata_rejected()

    def test_missing_overrides_does_not_deploy_bare_cli(self):
        (self.source / "skill-overrides.json").unlink()
        self.assert_metadata_rejected()

    def test_malformed_overrides_does_not_deploy_bare_cli(self):
        (self.source / "skill-overrides.json").write_text('{"name":', encoding="utf-8")
        self.assert_metadata_rejected()

    def test_non_object_overrides_safely_skips_deployment(self):
        (self.source / "skill-overrides.json").write_text("[]", encoding="utf-8")
        self.assert_metadata_rejected()

    def test_non_string_override_safely_skips_deployment(self):
        (self.source / "skill-overrides.json").write_text(
            json.dumps({"name": 42, "description": "Chrome only"}), encoding="utf-8"
        )
        self.assert_metadata_rejected()


class DeployBrowserUseSkillsTest(BrowserUseFixture):
    """--skills 部署 browser-use，保留跳过和强制完整刷新的语义。"""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(install, "REPO_ROOT", self.repo))
        install.generate_browser_use_skill(self.source, self.upstream)

    def run_skills_main(self, *extra_args):
        with (
            mock.patch.object(sys, "argv", [
                "install.py", "--skills", "--skills-dir", str(self.skills_dir),
                "--pi-agent-dir", str(self.root / "pi-agent"), *extra_args,
            ]),
            mock.patch.object(install, "check_prerequisites"),
            mock.patch.object(install, "deploy_browser_use") as tools,
        ):
            install.main()
        tools.assert_not_called()
        self.command.assert_not_called()
        self.run.assert_not_called()
        self.assert_only_browser_use_deployed()

    def test_skills_deploys_browser_use_by_name(self):
        self.run_skills_main("--name", "browser-use")
        self.assert_local_skill(self.destination)

    def test_skills_deploys_all_available_skills(self):
        self.run_skills_main()
        self.assert_local_skill(self.destination)

    def test_skills_skips_existing_deployment_without_force(self):
        self.seed_existing_deployment()
        existing = self.snapshot_files(self.destination)
        self.run_skills_main()
        self.assertEqual(self.snapshot_files(self.destination), existing)
        self.assertIn("跳过（已存在", self.output.getvalue())

    def test_skills_force_refreshes_complete_browser_use_deployment(self):
        self.seed_existing_deployment()
        self.run_skills_main("--name", "browser-use", "--force")
        self.assert_local_skill(self.destination)
        expected = {
            name: content for name, content in self.snapshot_files(self.source).items()
            if name not in {"skill-template.md", "skill-overrides.json"}
        }
        self.assertEqual(self.snapshot_files(self.destination), expected)


class BrowserHarnessCompatibilityTest(BrowserUseFixture):
    """兼容清单使用真实数据，版本探测不会启动实际 CLI。"""

    def test_verified_versions_do_not_warn(self):
        for version in self.versions:
            with self.subTest(version=version), mock.patch("builtins.print") as output:
                self.command.side_effect = None
                self.command.return_value = subprocess.CompletedProcess([], 0, version + "\n")
                install.warn_browser_harness_compatibility(self.source, self.harness)
                output.assert_not_called()
        self.command.assert_called_with(
            [str(self.harness), "--version"], capture_output=True, text=True, timeout=10
        )

    def test_unknown_version_warns_against_bare_cli_fallback(self):
        version = "999.0.0-unverified"
        self.assertNotIn(version, self.versions)
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0, version + "\n")
        install.warn_browser_harness_compatibility(self.source, self.harness)
        output = self.output.getvalue()
        self.assertIn(version, output)
        self.assertIn("不在适配验证列表", output)
        self.assertIn("不要回退到裸 CLI", output)

    def test_failed_version_command_warns_even_when_stdout_looks_supported(self):
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 1, self.versions[0] + "\n")
        install.warn_browser_harness_compatibility(self.source, self.harness)
        self.assertIn("unknown", self.output.getvalue())
        self.assertIn("不在适配验证列表", self.output.getvalue())

    def test_version_timeout_warns_without_aborting_sync(self):
        self.command.side_effect = subprocess.TimeoutExpired("browser-harness --version", 10)
        install.warn_browser_harness_compatibility(self.source, self.harness)
        self.assertIn("无法检查 Chrome 适配兼容性", self.output.getvalue())
        self.assertIn("chrome.py --doctor", self.output.getvalue())


class DeployBrowserUseFilesTest(BrowserUseFixture):
    """复制真实技能树到临时目录，验证运行文件及分发排除规则。"""

    def setUp(self):
        super().setUp()
        self.real_copy = self.root / "real-skill-copy"
        shutil.copytree(REAL_BROWSER_USE_SKILL, self.real_copy)

    def test_real_skill_preserves_scripts_compatibility_and_references(self):
        install.deploy_generic(self.real_copy, self.destination, force=True)
        self.assert_only_browser_use_deployed()
        for relative in (
            "SKILL.md", "scripts/chrome.py", "scripts/chrome_host.py",
            "scripts/chrome_runtime.py", "scripts/chrome_session.py", "scripts/compatibility.json",
            "references/upstream-skill.md",
            "references/page-workflow.md",
            "references/maintenance.md",
        ):
            with self.subTest(file=relative):
                self.assertTrue((self.real_copy / relative).is_file(), relative)
                self.assertTrue((self.destination / relative).is_file(), relative)
                self.assertEqual(
                    (self.destination / relative).read_bytes(), (self.real_copy / relative).read_bytes()
                )
        self.command.assert_not_called()
        self.run.assert_not_called()

    def test_real_skill_excludes_sync_metadata_and_python_cache_at_any_depth(self):
        excluded = (
            "skill-template.md", "skill-overrides.json", "__pycache__", "root.pyc",
            "scripts/__pycache__", "scripts/entry.pyc", "scripts/nested/__pycache__",
            "scripts/nested/helper.pyc", "references/__pycache__", "references/old.pyc",
        )
        for relative in excluded[2:]:
            target = self.real_copy / relative
            if target.name == "__pycache__":
                target.mkdir(parents=True, exist_ok=True)
                (target / "cache.pyc").write_bytes(b"cached")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"cached")
        install.deploy_generic(self.real_copy, self.destination, force=True)
        self.assert_only_browser_use_deployed()
        for relative in excluded:
            with self.subTest(file=relative):
                self.assertTrue((self.real_copy / relative).exists())
                self.assertFalse((self.destination / relative).exists(), relative)
        self.command.assert_not_called()
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
