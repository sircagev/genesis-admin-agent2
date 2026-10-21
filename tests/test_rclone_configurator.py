import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.commands import CommandError
from agent.rclone_configurator import RcloneConfigurator


class RcloneConfiguratorTest(unittest.TestCase):
    class FakeConfig:
        def __init__(self):
            self.data = {"scheduled_backup": {"rclone_remote": "anterior:ruta"}}
            self.saved = 0

        def save(self):
            self.saved += 1

    @staticmethod
    def _payload():
        return {
            "success": True,
            "remote_name": "odoo-backups",
            "remote_path": "odoo-backups",
            "backup_enabled": True,
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token": '{"access_token":"secret"}',
        }

    def test_configures_and_validates_remote_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            client = SimpleNamespace(rclone_config=lambda: self._payload())
            config = self.FakeConfig()
            with patch.object(RcloneConfigurator, "_path", return_value=path), patch(
                "agent.rclone_configurator.shutil.which",
                return_value="/usr/bin/rclone",
            ), patch(
                "agent.rclone_configurator.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                result = RcloneConfigurator(config, client=client).configure()

            self.assertTrue(result["success"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            content = path.read_text(encoding="utf-8")
            self.assertIn("[odoo-backups]", content)
            self.assertIn("token = {\"access_token\":\"secret\"}", content)
            run.assert_called_once()
            self.assertEqual(
                run.call_args.args[0], ["rclone", "lsd", "odoo-backups:odoo-backups"]
            )
            self.assertEqual(config.data["scheduled_backup"]["rclone_remote"], "odoo-backups:odoo-backups")
            self.assertTrue(config.data["scheduled_backup"]["enabled"])
            self.assertEqual(config.saved, 1)
            self.assertFalse(result["rclone_installed"])

    def test_restores_existing_configuration_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            original = "[existing]\ntype = drive\n"
            path.write_text(original, encoding="utf-8")
            client = SimpleNamespace(rclone_config=lambda: self._payload())
            config = self.FakeConfig()
            with patch.object(RcloneConfigurator, "_path", return_value=path), patch(
                "agent.rclone_configurator.shutil.which",
                return_value="/usr/bin/rclone",
            ), patch(
                "agent.rclone_configurator.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ):
                with self.assertRaises(CommandError):
                    RcloneConfigurator(config, client=client).configure()

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(config.data["scheduled_backup"]["rclone_remote"], "anterior:ruta")
            self.assertEqual(config.saved, 0)

    def test_rejects_invalid_remote_before_writing_configuration(self):
        values = self._payload()
        values["remote_name"] = "invalid remote"
        with self.assertRaises(CommandError):
            RcloneConfigurator._validate(values)

    def test_installs_rclone_before_configuring_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            client = SimpleNamespace(rclone_config=lambda: self._payload())
            config = self.FakeConfig()
            completed = SimpleNamespace(returncode=0)
            with patch.object(RcloneConfigurator, "_path", return_value=path), patch(
                "agent.rclone_configurator.os.geteuid",
                return_value=0,
            ), patch(
                "agent.rclone_configurator.shutil.which",
                side_effect=[None, "/usr/bin/apt-get", "/usr/bin/rclone"],
            ), patch(
                "agent.rclone_configurator.subprocess.run",
                side_effect=[completed, completed, completed],
            ) as run:
                result = RcloneConfigurator(config, client=client).configure()

            self.assertTrue(result["rclone_installed"])
            self.assertEqual(
                [call.args[0] for call in run.call_args_list],
                [
                    ["/usr/bin/apt-get", "update"],
                    ["/usr/bin/apt-get", "install", "-y", "rclone"],
                    ["rclone", "lsd", "odoo-backups:odoo-backups"],
                ],
            )
