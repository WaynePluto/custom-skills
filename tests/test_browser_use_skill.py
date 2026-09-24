"""browser-use 的生成、文档契约和示例控制流测试；不联网、不启动浏览器。"""

import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/browser-use"
sys.path.insert(0, str(ROOT / "scripts"))
import install


def python_examples(text):
    return re.findall(r"@'\n(.*?)\n'@ \| python", text, re.S)


class BrowserUseSkillTests(unittest.TestCase):
    """文档断言只覆盖关键契约，不能替代真实交互验收。"""

    @classmethod
    def setUpClass(cls):
        cls.template = (SKILL_DIR / "skill-template.md").read_text(encoding="utf-8")
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.maintenance = (SKILL_DIR / "references/maintenance.md").read_text(encoding="utf-8")
        cls.workflow = (SKILL_DIR / "references/page-workflow.md").read_text(encoding="utf-8")
        cls.overrides = json.loads((SKILL_DIR / "skill-overrides.json").read_text(encoding="utf-8"))

    def test_generated_skill_matches_template_and_reviewed_metadata(self):
        self.assertEqual(self.skill, install.apply_skill_overrides(self.template, self.overrides))
        self.assertEqual(self.overrides["name"], SKILL_DIR.name)
        self.assertEqual(SKILL_DIR.name, install.BROWSER_USE_SKILL)
        self.assertEqual(install.BROWSER_HARNESS, "browser-harness")
        self.assertIn("时使用", self.overrides["description"])
        self.assertIn("不管理 Chrome 以外的浏览器", self.overrides["description"])
        self.assertLessEqual(len(self.overrides["description"]), 1024)

    def test_dependency_and_skill_identity_are_distinct(self):
        self.assertIn("不是同名的 browser-use Python Agent 框架", self.skill)
        self.assertIn("browser-harness", self.skill)
        upstream = (SKILL_DIR / "references/upstream-skill.md").read_text(encoding="utf-8")
        self.assertRegex(upstream, r"(?m)^name: browser-harness$")
        self.assertNotIn("本地页面操作与恢复规则", upstream)

    def test_dependency_name_is_not_a_local_skill_identity(self):
        for parent in (ROOT / "skills", ROOT / "project-skills"):
            for path in parent.glob("*/SKILL.md"):
                with self.subTest(skill=path.parent.name):
                    self.assertNotEqual(path.parent.name, install.BROWSER_HARNESS)
                    frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
                    match = re.search(r"(?m)^name:\s*(.+)$", frontmatter)
                    self.assertIsNotNone(match)
                    self.assertNotEqual(match[1].strip().strip("\"'"), install.BROWSER_HARNESS)

    def test_skill_generation_deployment_and_test_names_use_capability_identity(self):
        self.assertTrue(callable(install.generate_browser_use_skill))
        self.assertTrue(callable(install.deploy_browser_use))
        dependency_functions = {name for name, value in vars(install).items()
                                if "browser_harness" in name and callable(value)}
        self.assertEqual(dependency_functions, {
            "browser_harness_bin", "warn_if_browser_harness_not_on_path",
            "warn_browser_harness_compatibility",
        })
        for name in ("test_browser_use_host.py", "test_browser_use_runtime.py",
                     "test_install_browser_use.py"):
            self.assertTrue((ROOT / "tests" / name).is_file(), name)

    def test_local_document_links_resolve_and_documents_are_bounded(self):
        paths = [SKILL_DIR / name for name in ("SKILL.md", "skill-template.md",
                                               "references/maintenance.md", "references/page-workflow.md")]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(text.splitlines()), 600, str(path))
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                if "://" not in link and not link.startswith("#"):
                    with self.subTest(document=path.name, link=link):
                        self.assertTrue((path.parent / link.split("#", 1)[0]).is_file())
        self.assertIn("references/page-workflow.md", self.skill)
        self.assertIn("references/upstream-skill.md", self.skill)

    def test_workflow_requires_identity_and_rejects_implicit_tab_adoption(self):
        for clause in ("ID、URL、标题", "不选第一个/最后一个标签", "不按同域名覆盖用户页面",
                       "不用 `ensure_real_tab()`", "不保留 Python 变量", "new_tab(url)"):
            self.assertIn(clause, self.skill)
        for clause in ("列表差集", "不是标签身份", "不证明业务结果", "close_tab(target_id)"):
            self.assertIn(clause, self.workflow)

    def test_fact_grounding_and_observation_cycle_are_explicit(self):
        for clause in ("Accessibility.getFullAXTree", "backendDOMNodeId", "不猜路径或元素名称",
                       "零匹配", "多匹配", "输入前确认焦点", "不复用过期节点或坐标",
                       "最多一次会改变页面状态的动作", "不默认同时抓树和截图"):
            self.assertIn(clause, self.skill)
        self.assertIn("CSS 视口坐标", self.workflow)
        self.assertIn("不靠固定 sleep 宣告完成", self.workflow)
        self.assertIn("True` 只说明文档加载条件满足", self.workflow)

    def test_popup_observation_precedes_retry_and_does_not_assume_ownership(self):
        for clause in ("同轮检查原页状态与标签列表", "URL 未变化不等于点击失败",
                       "仅列表新增不足以证明归属", "未确认前不再次点击"):
            self.assertIn(clause, self.skill)
        self.assertIn("Target.getTargets", self.workflow)
        self.assertIn("openerId", self.workflow)
        self.assertIn("不自动认领用户并行打开的页面", self.workflow)

    def test_uncertain_effects_never_imply_retry_or_rollback(self):
        for clause in ("timeout", "script_failed", "先核查页面", "不自动重发",
                       "不重跑含写操作的整段脚本", "不是只读沙箱", "须先向用户确认"):
            self.assertIn(clause, self.skill)
        for clause in ("不等于业务任务完成", "尚未执行", "不代表回滚", "不是驱动强制"):
            self.assertIn(clause, self.workflow)

    def test_connection_and_handoff_boundaries_remain_intact(self):
        for clause in ("approval_pending", "180 秒", "不轮询", "incompatible_version",
                       "不复制 Profile/Cookie", "不自动循环", "不随意切换前台",
                       "保留仍在等待用户登录", "不关闭 Chrome、其他浏览器或其他 session"):
            self.assertIn(clause, self.skill)
        self.assertIn("暂缓 stop", self.workflow)
        self.assertIn("这是依赖运行目录，不是技能安装目录", self.maintenance)
        self.assertIn("保持稳定以便识别和清理已有 session", self.maintenance)

    def test_session_tab_identity_and_first_navigation_contract(self):
        for text in (self.template, self.workflow):
            for clause in ("session_tab()", "dedicated_target_id", "Target.getTargetInfo",
                           "{targetId: str, url: str, title: str}", "type=page", "current_tab()",
                           "不创建、不切换、不导航", "不按 URL 或列表猜归属", "并行/第二页",
                           "session_tab_unsupported", "session_tab_unavailable", "script_failed",
                           "error_code", "output", "不自动重连", "不覆盖、不复制、不新建", "同 ID"):
                with self.subTest(clause=clause):
                    self.assertIn(clause, text)

    def test_dedicated_tab_cleanup_handoff_and_blank_boundaries(self):
        for text in (self.template, self.workflow):
            for clause in ("短暂创建一个 `about:blank`", "多余长期空白页", "硬隔离",
                           "默认 page 命令", "自动恢复一个新空白页", "不克隆", "暂缓"):
                with self.subTest(clause=clause):
                    self.assertIn(clause, text)
        for text in (self.template, self.workflow):
            for clause in ("固定到发出时的 CDP session", "stale-session", "并重放", "Target.*",
                           "显式 session", "原子"):
                with self.subTest(clause=clause):
                    self.assertIn(clause, text)
        self.assertIn("额外标签调用 `close_tab(target_id)`", self.workflow)
        self.assertIn("专用页由 `--stop` 清理", self.template)
        self.assertIn("不反复运行首导航示例", self.template)

    def test_maintenance_documents_local_injection_and_historical_validation(self):
        for clause in ("scripts/chrome_session.py", "scripts/chrome_runtime.py", "globals",
                       "PID、创建时间、generation、binding 和 token", "不替换官方页面 helpers",
                       "不修改第三方 `site-packages`", "0.1.13", "历史验证（技能改名时",
                       "不以 mock 测试代替真实矩阵"):
            self.assertIn(clause, self.maintenance)

    def test_all_powershell_python_examples_are_syntactically_valid(self):
        for filename, text in (("skill-template.md", self.template),):
            with self.subTest(filename=filename):
                [code] = python_examples(text)
                tree = ast.parse(code)
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name)]
                by_name = {name: [node for node in calls if node.func.id == name] for name in
                           ("session_tab", "switch_tab", "current_tab", "goto_url", "wait_for_load",
                            "page_info", "new_tab", "close_tab", "list_tabs", "ensure_real_tab")}
                for name in ("new_tab", "close_tab", "list_tabs", "ensure_real_tab"):
                    self.assertEqual(by_name[name], [], name)
                for name in ("session_tab", "current_tab", "page_info"):
                    [call] = by_name[name]
                    self.assertEqual(call.args, [])
                    self.assertEqual(call.keywords, [])
                [guard] = [node for node in tree.body if isinstance(node, ast.If)]
                self.assertEqual(ast.unparse(guard.test), "tab['url'] == 'about:blank'")
                self.assertEqual(guard.orelse, [])
                guarded = list(ast.walk(guard))
                for name in ("switch_tab", "current_tab", "goto_url", "wait_for_load", "page_info"):
                    [call] = by_name[name]
                    self.assertIn(call, guarded, name)
                self.assertEqual(ast.unparse(by_name["switch_tab"][0]), "switch_tab(tab['targetId'])")
                self.assertEqual(ast.unparse(by_name["wait_for_load"][0]), "wait_for_load(timeout=15)")
                self.assertFalse(any(isinstance(node, (ast.Try, ast.For, ast.While))
                                     for node in ast.walk(tree)))


class BrowserUseExamplesTests(unittest.TestCase):
    """只注入 mock helpers，核验专用页复用、拒绝覆盖和失败时保留归属。"""

    filenames = ("skill-template.md",)

    def run_example(self, filename, *, tab_url="about:blank", switched_target_id="owned-tab",
                    switched_url="about:blank", loaded=True, navigation_error=None,
                    session_error=None, switch_error=None):
        text = (SKILL_DIR / filename).read_text(encoding="utf-8")
        [code] = python_examples(text)
        calls = mock.Mock()
        calls.session_tab.return_value = {"targetId": "owned-tab", "url": tab_url, "title": "Task"}
        calls.session_tab.side_effect = session_error
        calls.switch_tab.side_effect = switch_error
        calls.current_tab.return_value = {"targetId": switched_target_id, "url": switched_url}
        calls.wait_for_load.return_value = loaded
        calls.goto_url.side_effect = navigation_error
        calls.page_info.return_value = {"url": "https://example.com/", "title": "Example Domain"}
        helpers = {name: getattr(calls, name) for name in
                   ("session_tab", "switch_tab", "current_tab", "goto_url", "wait_for_load",
                    "page_info", "new_tab", "close_tab", "list_tabs", "ensure_real_tab")}
        output = io.StringIO()
        error = None
        with redirect_stdout(output):
            try:
                exec(compile(code, filename, "exec"), helpers)
            except RuntimeError as exc:
                error = exc
        return calls, [json.loads(line) for line in output.getvalue().splitlines()], error

    def assert_no_tab_creation_cleanup_or_fallback(self, calls):
        calls.new_tab.assert_not_called()
        calls.close_tab.assert_not_called()
        calls.list_tabs.assert_not_called()
        calls.ensure_real_tab.assert_not_called()

    def test_blank_session_tab_is_recorded_rechecked_and_navigated_once(self):
        for filename in self.filenames:
            with self.subTest(filename=filename):
                calls, output, error = self.run_example(filename)
                self.assertIsNone(error)
                self.assertEqual(calls.mock_calls, [mock.call.session_tab(),
                    mock.call.switch_tab("owned-tab"), mock.call.current_tab(),
                    mock.call.goto_url("https://example.com"),
                    mock.call.wait_for_load(timeout=15), mock.call.page_info()])
                self.assertEqual(output[0], {"session_tab": calls.session_tab.return_value})
                self.assertTrue(output[1]["loaded"])
                self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_nonblank_session_tab_is_only_observed_and_never_navigated(self):
        for filename in self.filenames:
            for url in ("https://example.com/", "https://accounts.example/login",
                        "about:blank#changed", "chrome://newtab/", ""):
                with self.subTest(filename=filename, url=url):
                    calls, output, error = self.run_example(filename, tab_url=url)
                    self.assertIsNone(error)
                    self.assertEqual(calls.mock_calls, [mock.call.session_tab()])
                    self.assertEqual(output, [{"session_tab": calls.session_tab.return_value}])
                    self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_changed_target_id_or_url_after_switch_prevents_navigation(self):
        for filename in self.filenames:
            for target_id, url in (("other-tab", "about:blank"),
                                   ("owned-tab", "https://example.com/"),
                                   ("other-tab", "https://example.com/")):
                with self.subTest(filename=filename, target_id=target_id, url=url):
                    calls, output, error = self.run_example(
                        filename, switched_target_id=target_id, switched_url=url)
                    self.assertIsInstance(error, RuntimeError)
                    self.assertIn("Session tab changed", str(error))
                    self.assertEqual(calls.mock_calls, [mock.call.session_tab(),
                        mock.call.switch_tab("owned-tab"), mock.call.current_tab()])
                    self.assertEqual(output, [{"session_tab": calls.session_tab.return_value}])
                    self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_false_load_result_raises_without_retry_or_cleanup(self):
        for filename in self.filenames:
            with self.subTest(filename=filename):
                calls, output, error = self.run_example(filename, loaded=False)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("Page load was not confirmed", str(error))
                self.assertFalse(output[1]["loaded"])
                calls.session_tab.assert_called_once_with()
                calls.goto_url.assert_called_once_with("https://example.com")
                calls.wait_for_load.assert_called_once_with(timeout=15)
                calls.page_info.assert_called_once_with()
                self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_failed_navigation_preserves_session_tab_identity_without_retry(self):
        for filename in self.filenames:
            with self.subTest(filename=filename):
                failure = RuntimeError("Navigation failed")
                calls, output, error = self.run_example(filename, navigation_error=failure)
                self.assertIs(error, failure)
                self.assertEqual(output, [{"session_tab": calls.session_tab.return_value}])
                calls.goto_url.assert_called_once_with("https://example.com")
                calls.wait_for_load.assert_not_called()
                calls.page_info.assert_not_called()
                self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_failed_session_lookup_never_falls_back_to_another_tab(self):
        for filename in self.filenames:
            for code in ("session_tab_unsupported", "session_tab_unavailable", "unknown_daemon"):
                with self.subTest(filename=filename, code=code):
                    failure = RuntimeError(code)
                    calls, output, error = self.run_example(filename, session_error=failure)
                    self.assertIs(error, failure)
                    self.assertEqual(output, [])
                    self.assertEqual(calls.mock_calls, [mock.call.session_tab()])
                    self.assert_no_tab_creation_cleanup_or_fallback(calls)

    def test_failed_switch_preserves_identity_without_navigation_or_cleanup(self):
        for filename in self.filenames:
            with self.subTest(filename=filename):
                failure = RuntimeError("Switch failed")
                calls, output, error = self.run_example(filename, switch_error=failure)
                self.assertIs(error, failure)
                self.assertEqual(output, [{"session_tab": calls.session_tab.return_value}])
                self.assertEqual(calls.mock_calls, [mock.call.session_tab(),
                                                   mock.call.switch_tab("owned-tab")])
                self.assert_no_tab_creation_cleanup_or_fallback(calls)


if __name__ == "__main__":
    unittest.main()
