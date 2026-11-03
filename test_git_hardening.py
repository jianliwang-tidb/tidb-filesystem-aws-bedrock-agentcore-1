"""Regression tests for git command execution through repository-local config.

These tests use the real `git` binary and a temporary directory in place of
the Drive9 FUSE mount. They do not need AWS or Drive9 access.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from agentcore_test_stub import install as install_agentcore_test_stub

PROJECT_DIR = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_ID", "agent-a")
os.environ.setdefault("TEST_RUN_ID", "unit-test")
os.environ.setdefault("DRIVE9_TEST_TOKEN", "unit-test-token")
os.environ.setdefault(
    "AUTHZ_DISCOVERY_URL",
    "https://issuer.example/.well-known/openid-configuration",
)
os.environ.setdefault("AUTHZ_ALLOWED_CLIENTS", "unit-test-client")
install_agentcore_test_stub()


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


app = _load("agent_app", PROJECT_DIR / "app.py")


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class GitHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mount = Path(self._tmp.name) / "mnt"
        self.mount.mkdir()
        self.marker = Path(self._tmp.name) / "pwned"
        self._original_mounted = app._mounted
        app._mounted = lambda callback: {"status": "PASS", "callback": callback(self.mount)}

    def tearDown(self) -> None:
        app._mounted = self._original_mounted
        self._tmp.cleanup()

    @property
    def repo(self) -> Path:
        return self.mount / "git-workspaces" / "git-001"

    def _prepare(self) -> dict:
        result = app.action_git_prepare({"task_id": "git-001"})
        self.assertEqual(result["status"], "PASS")
        return result["callback"]

    def _append_config(self, text: str) -> None:
        with (self.repo / ".git" / "config").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def _hostile_command(self) -> str:
        return f"touch {self.marker}"

    def test_clean_repository_round_trip(self) -> None:
        prepared = self._prepare()
        self.assertEqual(prepared["branch"]["stdout"].strip(), "feature/git-001")
        self.assertEqual(prepared["head"]["exit_code"], 0)
        verified = app.action_git_verify({"task_id": "git-001"})["callback"]
        self.assertIn("?? dirty.txt", verified["status"]["stdout"])
        self.assertEqual(verified["dirty_content"], "uncommitted work from agent-a\n")
        # Re-running prepare on the existing repository must also be accepted.
        self._prepare()

    def test_fsmonitor_in_repo_config_is_refused(self) -> None:
        self._prepare()
        self._append_config(f"[core]\n\tfsmonitor = {self._hostile_command()}\n")
        with self.assertRaisesRegex(RuntimeError, "core.fsmonitor"):
            app.action_git_verify({"task_id": "git-001"})
        with self.assertRaisesRegex(RuntimeError, "core.fsmonitor"):
            app.action_git_prepare({"task_id": "git-001"})
        self.assertFalse(self.marker.exists())

    def test_other_command_keys_are_refused(self) -> None:
        hostile = (
            f'[filter "x"]\n\tclean = {self._hostile_command()}\n\tsmudge = {self._hostile_command()}\n',
            "[core]\n\thooksPath = /tmp\n",
            f"[core]\n\tsshCommand = {self._hostile_command()}\n",
            f"[alias]\n\tstatus = !{self._hostile_command()}\n",
            "[include]\n\tpath = /tmp/other.cfg\n",
            "[core]\n\tworktree = /\n",
            "[extensions]\n\tworktreeConfig = true\n",
            "[core]\n\tbare = true\n",
        )
        self._prepare()
        config = self.repo / ".git" / "config"
        clean = config.read_text(encoding="utf-8")
        for snippet in hostile:
            with self.subTest(snippet=snippet.splitlines()[0]):
                config.write_text(clean + snippet, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "refusing to run git"):
                    app.action_git_verify({"task_id": "git-001"})
        self.assertFalse(self.marker.exists())

    def test_hooks_do_not_run(self) -> None:
        self._prepare()
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\n{self._hostile_command()}\n", encoding="utf-8")
        hook.chmod(0o755)
        self._prepare()  # runs `git checkout -B`
        self.assertFalse(self.marker.exists())

    def test_redirecting_git_dir_is_refused(self) -> None:
        self._prepare()
        (self.repo / ".git" / "commondir").write_text("/tmp\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "commondir"):
            app.action_git_verify({"task_id": "git-001"})

    def test_gitfile_is_refused(self) -> None:
        self.repo.mkdir(parents=True)
        (self.repo / ".git").write_text("gitdir: /tmp/elsewhere\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not a directory"):
            app.action_git_verify({"task_id": "git-001"})

    def test_symlinked_workspace_and_files_are_refused(self) -> None:
        self._prepare()
        dirty = self.repo / "dirty.txt"
        dirty.unlink()
        secret = Path(self._tmp.name) / "local-secret"
        secret.write_text("local secret\n", encoding="utf-8")
        dirty.symlink_to(secret)
        with self.assertRaises(OSError):
            app.action_git_verify({"task_id": "git-001"})
        with self.assertRaises(OSError):
            app.action_git_prepare({"task_id": "git-001"})  # write must not follow the link
        self.assertEqual(secret.read_text(encoding="utf-8"), "local secret\n")

        other = Path(self._tmp.name) / "elsewhere"
        other.mkdir()
        shutil.rmtree(self.mount / "git-workspaces")
        (self.mount / "git-workspaces").symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            app.action_git_prepare({"task_id": "git-001"})

    def test_git_environment_has_no_credentials(self) -> None:
        os.environ["AWS_SECRET_ACCESS_KEY"] = "unit-test-aws"
        try:
            env = app._git_env()
        finally:
            del os.environ["AWS_SECRET_ACCESS_KEY"]
        self.assertNotIn("DRIVE9_API_KEY", env)
        self.assertFalse(any(key.startswith("AWS_") for key in env))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")


class RemotePathTests(unittest.TestCase):
    def test_git_metadata_paths_are_rejected(self) -> None:
        for path in (
            "git-workspaces/git-001/.git/config",
            "git-workspaces/git-001/.git",
            ".git/config",
            "a/.GIT/hooks/post-checkout",
            "./x/.Git/config",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, ".git"):
                    app._remote(path)

    def test_normal_paths_are_unchanged(self) -> None:
        self.assertEqual(app._remote("functional/a.txt"), f"{app.BASE_REMOTE}/functional/a.txt")
        self.assertEqual(app._remote("x/.gitignore"), f"{app.BASE_REMOTE}/x/.gitignore")
        self.assertEqual(app._remote("x/my.git/file"), f"{app.BASE_REMOTE}/x/my.git/file")


class GitConfigParserTests(unittest.TestCase):
    def test_parse_null_listing(self) -> None:
        output = "core.bare\nfalse\0user.name\nAgentCore Test\0core.fsmonitor\0"
        self.assertEqual(
            app._parse_git_config_list(output),
            [("core.bare", "false"), ("user.name", "AgentCore Test"), ("core.fsmonitor", "true")],
        )


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
@unittest.skipUnless((PROJECT_DIR / "app.py.bak-20261103").exists(), "pre-fix backup not present")
class PreFixReproductionTests(unittest.TestCase):
    """Shows the planted fsmonitor executed with the original code."""

    def test_original_code_executes_planted_fsmonitor(self) -> None:
        original = _load("agent_app_prefix", PROJECT_DIR / "app.py.bak-20261103")
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "mnt"
            mount.mkdir()
            marker = Path(tmp) / "pwned"
            original._mounted = lambda callback: {"status": "PASS", "callback": callback(mount)}
            original.action_git_prepare({"task_id": "git-001"})
            config = mount / "git-workspaces" / "git-001" / ".git" / "config"
            with config.open("a", encoding="utf-8") as handle:
                handle.write(f"[core]\n\tfsmonitor = touch {marker}\n")
            original.action_git_verify({"task_id": "git-001"})
            self.assertTrue(marker.exists(), "expected the pre-fix code to run the planted command")


if __name__ == "__main__":
    unittest.main()
