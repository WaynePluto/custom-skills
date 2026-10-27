#!/usr/bin/env python3
"""install.py context 部署逻辑的静态测试（不联网、不改真实全局目录）。

运行：python -m unittest discover -s tests -p 'test_*.py'
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install  # noqa: E402


class RepoContextFileTest(unittest.TestCase):
    """仓库静态约束：DSH_AGENTS.md 必须存在且包含节选的四节标题。"""

    EXPECTED_SECTIONS = [
        "## Windows 命令执行环境（`pwsh` 工具）",
        "## 需求澄清与决策对齐",
        "## 注释与文档规范",
        "## 回答风格",
    ]

    def test_dsh_agents_md_contains_expected_sections(self):
        source = install.REPO_ROOT / "context" / "DSH_AGENTS.md"
        self.assertTrue(source.is_file(), f"缺少 {source}")
        text = source.read_text(encoding="utf-8")
        for heading in self.EXPECTED_SECTIONS:
            self.assertIn(heading, text, f"缺少节标题：{heading}")

    def test_dsh_context_files_mapping(self):
        """DSH_AGENTS.md 必须映射为 AGENTS.md，否则 DSH 读不到部署结果。"""
        self.assertIn("DSH_AGENTS.md", install.DSH_CONTEXT_FILES)
        self.assertEqual(install.DSH_CONTEXT_FILES["DSH_AGENTS.md"], "AGENTS.md")


class DeployContextTest(unittest.TestCase):
    """deploy_context：Pi 目录只接收非 DSH 专用文件。"""

    def _make_repo(self, root: Path, *files: str) -> None:
        context = root / "context"
        context.mkdir(parents=True)
        for name in files:
            (context / name).write_text(f"# {name}\n", encoding="utf-8")

    def test_dsh_file_not_deployed_to_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root, "APPEND_SYSTEM.md", "DSH_AGENTS.md")
            pi_dir = root / "pi"
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                install.deploy_context(pi_dir, force=True)
            self.assertTrue((pi_dir / "APPEND_SYSTEM.md").exists())
            self.assertFalse((pi_dir / "DSH_AGENTS.md").exists())

    def test_existing_file_skipped_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root, "APPEND_SYSTEM.md")
            pi_dir = root / "pi"
            pi_dir.mkdir()
            (pi_dir / "APPEND_SYSTEM.md").write_text("old", encoding="utf-8")
            buffer = io.StringIO()
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new=buffer),
            ):
                install.deploy_context(pi_dir, force=False)
            self.assertEqual(
                (pi_dir / "APPEND_SYSTEM.md").read_text(encoding="utf-8"), "old"
            )
            self.assertIn("--force", buffer.getvalue())

    def test_missing_context_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pi_dir = root / "pi"
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                install.deploy_context(pi_dir, force=True)
            self.assertFalse(pi_dir.exists())


class DeployDshContextTest(unittest.TestCase):
    """deploy_dsh_context：DSH_AGENTS.md → <dsh-dir>/AGENTS.md，沿用 --force 语义。"""

    def _make_repo(self, root: Path, content: str = "# dsh rules\n") -> None:
        context = root / "context"
        context.mkdir(parents=True)
        (context / "DSH_AGENTS.md").write_text(content, encoding="utf-8")

    def test_deploys_with_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root)
            dsh_dir = root / "dsh"
            buffer = io.StringIO()
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new=buffer),
            ):
                install.deploy_dsh_context(dsh_dir, force=False)
            dest = dsh_dir / "AGENTS.md"
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(encoding="utf-8"), "# dsh rules\n")
            self.assertIn("DSH_AGENTS.md →", buffer.getvalue())

    def test_existing_dest_skipped_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root, content="new")
            dsh_dir = root / "dsh"
            dsh_dir.mkdir()
            (dsh_dir / "AGENTS.md").write_text("old", encoding="utf-8")
            buffer = io.StringIO()
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new=buffer),
            ):
                install.deploy_dsh_context(dsh_dir, force=False)
            self.assertEqual((dsh_dir / "AGENTS.md").read_text(encoding="utf-8"), "old")
            self.assertIn("--force", buffer.getvalue())

    def test_existing_dest_overwritten_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root, content="new")
            dsh_dir = root / "dsh"
            dsh_dir.mkdir()
            (dsh_dir / "AGENTS.md").write_text("old", encoding="utf-8")
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                install.deploy_dsh_context(dsh_dir, force=True)
            self.assertEqual((dsh_dir / "AGENTS.md").read_text(encoding="utf-8"), "new")

    def test_missing_source_is_noop(self):
        """源文件缺失时不得创建 DSH 目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "context").mkdir()
            dsh_dir = root / "dsh"
            with (
                mock.patch.object(install, "REPO_ROOT", root),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                install.deploy_dsh_context(dsh_dir, force=True)
            self.assertFalse(dsh_dir.exists())


if __name__ == "__main__":
    unittest.main()
