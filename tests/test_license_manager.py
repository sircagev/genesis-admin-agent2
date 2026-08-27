import unittest
from pathlib import Path
from unittest.mock import patch

from agent.commands import CommandError
from agent.license_manager import OdooLicenseManager


class FakeConfig:
    def get(self, _key, default=None):
        return default

    def section(self, _key):
        return {}


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        return {"success": True, "returncode": 0, "output": ""}


class OdooLicenseManagerTest(unittest.TestCase):
    API_KEY = "generated_api_key_that_must_not_be_logged"

    def setUp(self):
        self.runner = FakeRunner()
        self.progress = []
        self.manager = OdooLicenseManager(
            FakeConfig(),
            runner=self.runner,
            progress_callback=lambda stage, percent, message: self.progress.append(
                (stage, percent, message)
            ),
            status_reader=lambda _unit: {"active_state": "active"},
        )
        self.context = {
            "service": {"unit": "odoo-server-customer.service"},
            "database": "customer",
            "runtime": {
                "version": "17",
                "python": Path("/opt/customer/venv/bin/python"),
                "odoo_bin": Path("/opt/customer/odoo-bin"),
                "odoo_root": Path("/opt/customer"),
                "config": Path("/etc/odoocustomer.conf"),
            },
        }

    def _bootstrap_result(self):
        return {
            "success": True,
            "module_installed": True,
            "module_state": "installed",
            "api_key": self.API_KEY,
            "company_count": 2,
        }

    def test_installed_module_keeps_running_and_returns_existing_key(self):
        with (
            patch.object(self.manager, "_context", return_value=self.context),
            patch.object(
                self.manager,
                "_run_tool",
                side_effect=[
                    {"module_installed": True, "module_state": "installed"},
                    self._bootstrap_result(),
                ],
            ),
        ):
            result = self.manager.bootstrap({})

        self.assertEqual(result["api_key"], self.API_KEY)
        self.assertFalse(result["installed_by_job"])
        self.assertFalse(
            any(command[:2] == ["systemctl", "stop"] for command in self.runner.commands)
        )
        self.assertNotIn(self.API_KEY, str(self.progress))

    def test_missing_module_is_installed_with_service_boundary(self):
        with (
            patch.object(self.manager, "_context", return_value=self.context),
            patch.object(
                self.manager,
                "_run_tool",
                side_effect=[
                    {"module_installed": False, "module_state": "uninstalled"},
                    self._bootstrap_result(),
                ],
            ),
        ):
            result = self.manager.bootstrap({})

        self.assertTrue(result["installed_by_job"])
        self.assertEqual(self.runner.commands[0][:2], ["systemctl", "stop"])
        install = self.runner.commands[1]
        self.assertIn("-i", install)
        self.assertIn("account_invoicing_limit", install)
        self.assertEqual(self.runner.commands[-1][:2], ["systemctl", "start"])

    def test_only_fixed_license_module_is_accepted(self):
        with self.assertRaises(CommandError):
            self.manager._context(
                {
                    "module_name": "arbitrary_module",
                    "service_name": "odoo-server-customer.service",
                    "database_name": "customer",
                }
            )


if __name__ == "__main__":
    unittest.main()
