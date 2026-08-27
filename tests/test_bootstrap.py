import types
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from agent.bootstrap import (
    DEVELOPMENT_PACKAGES,
    EXECUTABLE_COMPONENTS,
    REQUIRED_COMPONENTS,
    ServerBootstrapAuditor,
    collect_bootstrap_inventory,
)
from agent.server_configurator import ServerConfigurator
from agent.commands import run as command_run
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


    def test_command_runner_forwards_stdin_without_returning_it(self):
        process = types.SimpleNamespace(
            stdout="",
            returncode=0,
        )
        with patch(
            "agent.commands.subprocess.run",
            return_value=process,
        ) as subprocess_run:
            result = command_run(
                ["chpasswd"],
                input_data="root:secret-only-stdin\n",
            )

        self.assertEqual(
            subprocess_run.call_args.kwargs["input"],
            "root:secret-only-stdin\n",
        )
        self.assertNotIn("secret-only-stdin", str(result))

    def test_install_never_runs_distribution_upgrade(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return {"success": True, "output": "ok"}

        manager = ServerConfigurator()
        with patch.object(manager, "_require_root"), patch.object(
            manager, "_ensure_node_20"
        ), patch.object(manager, "_ensure_wkhtmltopdf"), patch.object(
            manager, "_ensure_odoo_user"
        ), patch.object(manager, "_ensure_postgresql_scram"), patch(
            "agent.server_configurator.run", side_effect=fake_run
        ), patch(
            "agent.server_configurator.ServerBootstrapAuditor.audit",
            return_value={
                "success": True,
                "ready": True,
                "components": {},
            },
        ):
            result = manager.install({})

        self.assertTrue(result["success"])
        self.assertFalse(result["apt_upgrade_executed"])
        self.assertFalse(result["postgres_role_created"])
        self.assertFalse(
            any(
                command[:2] in (["apt", "upgrade"], ["apt-get", "upgrade"])
                for command in commands
            )
        )

    def test_root_password_uses_stdin_and_never_command_arguments(self):
        secret = "Root-Password-Only-In-Stdin"
        manager = ServerConfigurator()
        manager.set_runtime_config({"security_root_password": secret})
        command = MagicMock(return_value={"success": True, "output": ""})
        with patch("agent.server_configurator.run", command):
            manager._set_root_password()

        args, kwargs = command.call_args
        self.assertEqual(args[0], ["chpasswd"])
        self.assertEqual(kwargs["input_data"], f"root:{secret}\n")
        self.assertNotIn(secret, " ".join(args[0]))

    def test_prepare_security_retains_current_port_and_hides_password(self):
        manager = ServerConfigurator()
        manager.set_runtime_config(
            {"security_root_password": "Root-Password-Only-In-Memory"}
        )
        payload = {
            "ssh_port": 44551,
            "authorized_keys": "ssh-ed25519 QUFBQQ== genesis",
            "ufw_enabled": True,
            "password_auth": False,
            "permit_root": True,
        }
        with patch.object(manager, "_require_root"), patch.object(
            manager, "_current_ssh_ports", return_value=[22]
        ), patch.object(manager, "_install_root_keys"), patch.object(
            manager, "_set_root_password"
        ), patch.object(manager, "_configure_ufw"), patch.object(
            manager, "_write_ssh_profile"
        ) as profile, patch.object(
            manager, "_write_socket_profile", return_value=False
        ), patch.object(
            manager, "_port_is_listening", return_value=True
        ), patch.object(manager, "_reload_ssh"), patch(
            "agent.server_configurator.run",
            return_value={"success": True, "output": ""},
        ):
            result = manager.prepare_security(payload)

        profile.assert_called_once_with([22, 44551])
        self.assertEqual(result["previous_ssh_ports"], [22])
        self.assertEqual(result["target_ssh_port"], 44551)
        self.assertTrue(result["requires_confirmation"])
        self.assertNotIn(
            "Root-Password-Only-In-Memory",
            str(result),
        )

    def test_executor_exposes_only_typed_configuration_jobs(self):
        executor = JobExecutor.__new__(JobExecutor)
        executor.server_configurator = types.SimpleNamespace(
            install=lambda payload: {"action": "install", "payload": payload},
            prepare_security=lambda payload: {
                "action": "prepare",
                "payload": payload,
            },
            enforce_security=lambda payload: {
                "action": "enforce",
                "payload": payload,
            },
        )
        self.assertEqual(
            executor.bootstrap_install({})["action"], "install"
        )
        self.assertEqual(
            executor.security_prepare({})["action"], "prepare"
        )
        self.assertEqual(
            executor.security_enforce({})["action"], "enforce"
        )


if __name__ == "__main__":
    unittest.main()
