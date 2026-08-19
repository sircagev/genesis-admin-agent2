import types
import unittest
from unittest.mock import patch

from agent.bootstrap import (
    DEVELOPMENT_PACKAGES,
    EXECUTABLE_COMPONENTS,
    REQUIRED_COMPONENTS,
    ServerBootstrapAuditor,
    collect_bootstrap_inventory,
)
from agent.executor import JobExecutor


class BootstrapAuditTest(unittest.TestCase):
    def test_lightweight_inventory_reports_each_component_without_apt(self):
        def executable(name):
            return f"/usr/bin/{name}"

        account = types.SimpleNamespace(pw_dir="/opt/odoo", pw_uid=120, pw_gid=120)
        group = types.SimpleNamespace(gr_name="odoo")
        with patch("agent.bootstrap.shutil.which", side_effect=executable), patch(
            "agent.bootstrap.Path.exists", return_value=True
        ), patch("agent.bootstrap.pwd.getpwnam", return_value=account), patch(
            "agent.bootstrap.grp.getgrgid", return_value=group
        ):
            result = collect_bootstrap_inventory()

        self.assertFalse(result["ready"])
        self.assertEqual(
            result["components"]["development_libraries"]["status"],
            "missing",
        )
        self.assertFalse(result["apt_upgrade_allowed"])
        self.assertTrue(
            all(
                result["components"][key]["status"] == "installed"
                for key in EXECUTABLE_COMPONENTS
            )
        )

    def test_missing_components_generate_plan_but_never_install(self):
        inventory = {
            "ready": False,
            "components": {
                key: {"status": "missing"}
                for key in (
                    *EXECUTABLE_COMPONENTS,
                    "development_libraries",
                    "certbot_nginx",
                    "odoo_user",
                )
            },
            "required_components": [],
            "apt_upgrade_allowed": False,
        }
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return {"success": False, "output": ""}

        with patch(
            "agent.bootstrap.collect_bootstrap_inventory",
            return_value=inventory,
        ), patch("agent.bootstrap.run", side_effect=fake_run):
            result = ServerBootstrapAuditor().audit({})

        self.assertTrue(result["success"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["installation_plan"])
        self.assertFalse(result["apt_upgrade_allowed"])
        self.assertFalse(
            any(command and command[0] in ("apt", "apt-get") for command in commands)
        )
        queried_packages = {
            command[-1]
            for command in commands
            if command and command[0] == "dpkg-query"
        }
        self.assertEqual(queried_packages, set(DEVELOPMENT_PACKAGES))

    def test_freetype_package_names_are_compatible(self):
        inventory = {
            "ready": False,
            "components": {
                key: {"status": "missing"}
                for key in REQUIRED_COMPONENTS
            },
            "required_components": list(REQUIRED_COMPONENTS),
            "apt_upgrade_allowed": False,
        }

        def fake_run(command, **_kwargs):
            if command and command[0] == "dpkg-query":
                installed = command[-1] != "libfreetype6-dev"
                return {
                    "success": installed,
                    "output": "install ok installed" if installed else "",
                }
            return {"success": False, "output": ""}

        with patch(
            "agent.bootstrap.collect_bootstrap_inventory",
            return_value=inventory,
        ), patch("agent.bootstrap.run", side_effect=fake_run):
            result = ServerBootstrapAuditor().audit({})

        libraries = result["components"]["development_libraries"]
        self.assertEqual(libraries["status"], "installed")
        self.assertIn("libfreetype-dev", libraries["installed"])
        self.assertNotIn(
            "libfreetype6-dev | libfreetype-dev",
            libraries["missing"],
        )

    def test_executor_exposes_only_typed_bootstrap_audit(self):
        executor = JobExecutor.__new__(JobExecutor)
        executor.bootstrap_auditor = types.SimpleNamespace(
            audit=lambda payload: {"success": True, "payload": payload}
        )
        result = executor.bootstrap_audit({"request": "bootstrap.audit"})
        self.assertTrue(result["success"])
        self.assertEqual(result["payload"]["request"], "bootstrap.audit")


if __name__ == "__main__":
    unittest.main()
