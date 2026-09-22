"""computer-use Skill 文档契约测试；不执行桌面操作。"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/computer-use"


def frontmatter(text):
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise AssertionError("SKILL.md 缺少 YAML frontmatter")
    values = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


class ComputerUseSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = SKILL_DIR / "SKILL.md"
        cls.skill = cls.path.read_text(encoding="utf-8")
        cls.meta = frontmatter(cls.skill)
        cls.documents = {
            path.relative_to(SKILL_DIR).as_posix(): path.read_text(encoding="utf-8")
            for path in SKILL_DIR.rglob("*.md")
        }
        cls.all_text = "\n".join(cls.documents.values())

    def test_identity_and_description_define_trigger_and_exclusion(self):
        self.assertEqual(self.meta.get("name"), "computer-use")
        description = self.meta.get("description", "")
        self.assertLessEqual(len(description), 1024)
        self.assertRegex(description, r"Windows|桌面|GUI")
        self.assertRegex(description, r"原生窗口|系统.*对话框|桌面应用")
        self.assertRegex(description, r"不适用|不用于|不要用于|优先")
        self.assertRegex(description, r"网页|浏览器|DOM|CDP")

    def test_skill_excludes_non_gui_and_browser_workflows(self):
        for clause in ("browser-use", "Shell", "Registry", "任意 Python", "文件系统",
                       "进程终止", "程序启动", "MCP Server", "常驻"):
            with self.subTest(clause=clause):
                self.assertIn(clause, self.all_text)
        self.assertRegex(self.skill, r"网页.*browser-use|browser-use.*网页")

    def test_observe_act_verify_workflow_uses_fresh_single_use_snapshots(self):
        for clause in ("windows", "focus", "snapshot", "request-id", "UUID", "单次动作",
                       "重新观察", "snapshot_consumed", "cleanup"):
            with self.subTest(clause=clause):
                self.assertIn(clause, self.all_text)
        self.assertRegex(self.all_text, r"不.*复用.*快照|快照.*不可.*复用")
        self.assertRegex(self.all_text, r"不自动重试|不自动重发")
        self.assertRegex(self.all_text, r"uncertain")

    def test_sensitive_operations_and_untrusted_screen_content_are_explicit(self):
        for clause in ("支付", "删除", "发布", "发送", "安装", "授权", "用户确认",
                       "UAC", "锁屏", "密码", "MFA", "验证码", "不可信"):
            with self.subTest(clause=clause):
                self.assertIn(clause, self.all_text)
        self.assertRegex(self.all_text, r"窗口.*文本|UIA.*文本|截图")
        self.assertRegex(self.all_text, r"不得执行|不能.*授权|不.*视为.*授权")

    def test_input_output_and_privacy_boundaries_are_documented(self):
        for clause in ("stdin", "400", "剪贴板", "单个 JSON", "side_effect", "verified",
                       "不记录", "正文", "timeout"):
            with self.subTest(clause=clause):
                self.assertIn(clause, self.all_text)
        self.assertRegex(self.all_text, r"不.*base64|base64.*不")
        self.assertRegex(self.all_text, r"超时.*uncertain|uncertain.*超时")

    def test_local_markdown_links_resolve_and_all_documents_are_bounded(self):
        self.assertIn("references/workflow.md", self.skill)
        self.assertIn("references/safety.md", self.skill)
        self.assertIn("references/upstream.md", self.skill)
        self.assertTrue(self.documents)
        for relative, text in self.documents.items():
            path = SKILL_DIR / relative
            with self.subTest(document=relative):
                self.assertLessEqual(len(text.splitlines()), 600)
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                target = target.strip().split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(document=relative, link=target):
                    self.assertTrue((path.parent / target).resolve().is_file())

    def test_runtime_dependency_and_source_are_pinned(self):
        lock = (SKILL_DIR / "scripts" / "requirements.lock").read_text(encoding="utf-8")
        compatibility = (SKILL_DIR / "scripts" / "compatibility.json").read_text(encoding="utf-8")
        self.assertIn("cua-driver==0.28.2", lock)
        self.assertIn("--hash=sha256:", lock)
        self.assertNotIn("cua-driver>=", lock)
        self.assertIn("fc188250b4ca8549b8e61f937fdb1fb560770e86", compatibility)
        self.assertIn("package_code_and_native_sha256", compatibility)

    def test_background_boundary_is_explicit(self):
        for clause in ("background", "foreground", "后台", "不自动", "cua-driver", "PostMessage"):
            self.assertIn(clause, self.all_text)
        self.assertIn("interference_detected", self.all_text)


    def test_cli_examples_do_not_offer_arbitrary_code_or_command_execution(self):
        code_blocks = re.findall(r"```(?:powershell|pwsh)?\n(.*?)```", self.skill, re.S | re.I)
        examples = "\n".join(code_blocks)
        self.assertIn("computer.py", examples)
        for forbidden in ("-c ", "--eval", "--script", "--shell", "cmd.exe", "powershell.exe"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, examples)


if __name__ == "__main__":
    unittest.main()
