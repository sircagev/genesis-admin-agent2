import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.commands import CommandError
from agent.updater import AgentUpdater


CURRENT_SHA = "1" * 40
TARGET_SHA = "2" * 40


class FakeConfig:
    def section(self, key):
        if key == "update":
            return {
                "repository": "sircagev/genesis-admin-agent2",
                "branch": "main",
            }
        return {}


class FakeRunner:
    def __init__(self, dirty=False, current_sha=CURRENT_SHA):
        self.commands = []
        self.dirty = dirty
        self.current_sha = current_sha

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        joined = " ".join(command)
        if "remote get-url" in joined:
            return {
                "success": True,
                "output": "https://github.com/sircagev/genesis-admin-agent2.git",
            }
        if "symbolic-ref --quiet --short HEAD" in joined:
            return {"success": True, "output": "main"}
        if "rev-parse HEAD" in joined:
            return {"success": True, "output": self.current_sha}
        if "rev-parse origin/main" in joined:
            return {"success": True, "output": TARGET_SHA}
        if f"show {TARGET_SHA}:agent/__init__.py" in joined:
            return {"success": True, "output": '__version__ = "2.9.0"'}
        if f"show {TARGET_SHA}:requirements.txt" in joined:
            return {"success": True, "output": "requests>=2.31,<3"}
        if "status --porcelain=v1" in joined:
            return {
                "success": True,
                "output": " M agent/main.py" if self.dirty else "",
            }
        return {"success": True, "output": ""}


class AgentUpdaterTest(unittest.TestCase):
    def _updater(self, runner):
        updater = AgentUpdater(
            FakeConfig(),
            base_path="/opt/genesis-admin-agent",
            runner=runner,
            current_version="2.8.0",
        )
        updater._repository_status = types.MethodType(
            lambda _self: None,
            updater,
        )
        return updater

    def test_check_reports_newer_version_without_installing(self):
        runner = FakeRunner()
        result = self._updater(runner).check({})

        self.assertTrue(result["update_available"])
        self.assertEqual(result["available_version"], "2.9.0")
        commands = [" ".join(command) for command in runner.commands]
        self.assertFalse(any("pip install" in command for command in commands))
        self.assertFalse(any(" merge " in f" {command} " for command in commands))
        self.assertFalse(any("reset --hard" in command for command in commands))

    def test_apply_rejects_dirty_repository_without_discarding_changes(self):
        runner = FakeRunner(dirty=True)
        updater = self._updater(runner)

        with self.assertRaises(CommandError) as raised:
            updater.apply(
                {
                    "target_sha": TARGET_SHA,
                    "target_version": "2.9.0",
                }
            )

        self.assertIn("cambios locales", str(raised.exception))
        commands = [" ".join(command) for command in runner.commands]
        self.assertFalse(any("reset --hard" in command for command in commands))
        self.assertFalse(any(" merge " in f" {command} " for command in commands))

    def test_apply_uses_fast_forward_and_requests_delayed_restart(self):
        runner = FakeRunner()
        updater = self._updater(runner)
        with patch.object(
            updater,
            "_refresh_service_definition",
            return_value=None,
        ):
            result = updater.apply(
                {
                    "target_sha": TARGET_SHA,
                    "target_version": "2.9.0",
                }
            )

        self.assertTrue(result["updated"])
        self.assertTrue(result["restart_required"])
        commands = [" ".join(command) for command in runner.commands]
        self.assertTrue(any("merge --ff-only" in command for command in commands))
        self.assertTrue(any("pip install --upgrade" in command for command in commands))
        self.assertFalse(any("reset --hard" in command for command in commands))

    def test_new_code_with_old_process_requests_restart_only(self):
        runner = FakeRunner(current_sha=TARGET_SHA)
        updater = self._updater(runner)

        checked = updater.check({})
        with patch.object(
            updater,
            "_refresh_service_definition",
            return_value=None,
        ):
            applied = updater.apply(
                {
                    "target_sha": TARGET_SHA,
                    "target_version": "2.9.0",
                }
            )

        self.assertFalse(checked["update_available"])
        self.assertTrue(checked["restart_required"])
        self.assertFalse(applied["updated"])
        self.assertTrue(applied["restart_required"])
        commands = [" ".join(command) for command in runner.commands]
        self.assertTrue(any("pip install" in command for command in commands))
        self.assertFalse(any(" merge " in f" {command} " for command in commands))

    def test_repository_url_with_embedded_identity_is_not_returned(self):
        identity = AgentUpdater._repository_identity(
            "https://token@github.com/sircagev/genesis-admin-agent2.git"
        )
        self.assertEqual(identity, "")

    def test_repository_status_rejects_wrong_checked_out_branch(self):
        runner = FakeRunner()

        def branch_runner(command, **kwargs):
            if "symbolic-ref --quiet --short HEAD" in " ".join(command):
                return {"success": True, "output": "development"}
            return runner(command, **kwargs)

        updater = AgentUpdater(
            FakeConfig(),
            base_path="/opt/genesis-admin-agent",
            runner=branch_runner,
            current_version="2.8.0",
        )
        with patch.object(Path, "is_dir", return_value=True):
            with self.assertRaises(CommandError) as raised:
                updater._repository_status()

        self.assertIn("rama instalada", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
