import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from agent.client import ControllerClient
from agent.commands import CommandError
from agent.module_manager import OdooModuleManager
from agent.odoo_module_tool import _module_values


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


class OdooModuleManagerTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.manager = OdooModuleManager(
            FakeConfig(),
            runner=self.runner,
        )

    def test_repository_rejects_embedded_credentials(self):
        with self.assertRaises(CommandError):
            self.manager._validate_repository(
                {
                    "url": (
                        "https://token@github.com/"
                        "evopos30/modulosFE19.git"
                    ),
                    "branch": "cristian_local",
                    "subpaths": ["custom_addons", "modulos"],
                }
            )

    def test_steps_are_typed_and_ordered(self):
        steps = self.manager._validate_steps(
            [
                {
                    "sequence": 20,
                    "action": "upgrade",
                    "module": "module_b",
                    "phase": "after_code",
                },
                {
                    "sequence": 10,
                    "action": "uninstall",
                    "module": "module_a",
                },
            ]
        )
        self.assertEqual(
            [step["module"] for step in steps],
            ["module_a", "module_b"],
        )
        self.assertEqual(steps[0]["phase"], "before_code")

    def test_install_cannot_run_before_code(self):
        with self.assertRaises(CommandError):
            self.manager._validate_steps(
                [
                    {
                        "action": "install",
                        "module": "module_a",
                        "phase": "before_code",
                    }
                ]
            )

    def test_dependency_closure_is_recursive(self):
        catalog = {
            "module_a": {"dependencies": ["module_b", "base"]},
            "module_b": {"dependencies": ["module_c"]},
            "module_c": {"dependencies": []},
        }
        self.assertEqual(
            self.manager._dependency_closure("module_a", catalog),
            ["base", "module_b", "module_c"],
        )

    def test_plan_reports_dirty_and_credential_blockers(self):
        payload = {
            "steps": [
                {
                    "sequence": 10,
                    "action": "upgrade",
                    "module": "module_a",
                    "phase": "after_code",
                }
            ]
        }
        context = {
            "database": "customer",
            "service": {"unit": "odoo-server-customer.service"},
            "runtime": {"version": "19"},
            "repo": {"path": Path("/opt/customer/repo")},
            "repository": {
                "branch": "main",
                "subpaths": ["modulos"],
            },
        }
        inventory = {
            "modules": [
                {
                    "name": "module_a",
                    "state": "installed",
                    "installed_version": "19.0.1.0.0",
                    "dependencies": ["base"],
                }
            ]
        }
        with (
            patch.object(self.manager, "_context", return_value=context),
            patch.object(
                self.manager,
                "_repo_state",
                return_value={
                    "head": "1" * 40,
                    "branch": "main",
                    "dirty": True,
                    "credential_in_origin": True,
                },
            ),
            patch.object(
                self.manager,
                "_fetch_target",
                return_value="2" * 40,
            ),
            patch.object(
                self.manager,
                "_target_catalog",
                return_value={
                    "module_a": {
                        "name": "module_a",
                        "version": "19.0.2.0.0",
                        "installable": True,
                        "dependencies": ["base"],
                        "external_dependencies": {},
                        "summary": "A",
                        "application": False,
                        "auto_install": False,
                        "subpath": "modulos",
                    }
                },
            ),
            patch.object(
                self.manager,
                "_run_tool",
                return_value=inventory,
            ),
            patch.object(
                self.manager,
                "_git_local",
                return_value={"success": True, "output": ""},
            ),
        ):
            result = self.manager.plan(payload)

        self.assertFalse(result["executable"])
        self.assertIn(
            "El repositorio tiene cambios locales.",
            result["blockers"],
        )
        self.assertIn(
            "El origin contiene credenciales.",
            result["blockers"],
        )
        self.assertEqual(len(result["plan_fingerprint"]), 64)
        self.assertFalse(
            any(" merge " in f" {' '.join(command)} " for command in self.runner.commands)
        )

    def test_manual_upgrade_of_uninstalled_module_is_ignored_in_plan(self):
        catalog = {
            "module_a": {
                "version": "19.0.1.0.0",
                "installable": True,
                "dependencies": [],
            }
        }
        steps = self.manager._validate_steps(
            [{"action": "upgrade", "module": "module_a"}]
        )

        expanded, blockers, proposed, _requested = (
            self.manager._expand_versioned_steps(
                steps,
                "manual",
                catalog,
                {},
                [],
                {},
            )
        )

        self.assertFalse(blockers)
        self.assertEqual(expanded, [])
        self.assertEqual(proposed, {})

    def test_upgrade_of_uninstalled_module_is_skipped_at_execution(self):
        context = {"database": "customer", "runtime": {"version": "19"}}
        with patch.object(
            self.manager,
            "_module_state",
            return_value={"state": "uninstalled"},
        ):
            result = self.manager._execute_step(
                context,
                {"action": "upgrade", "module": "module_a"},
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_installed")
        self.assertEqual(self.runner.commands, [])

    def test_odoo19_install_uses_official_module_cli(self):
        context = {
            "database": "customer",
            "runtime": {
                "version": "19",
                "python": Path("/opt/customer/venv/bin/python"),
                "odoo_bin": Path("/opt/customer/odoo-bin"),
                "config": Path("/etc/odoocustomer.conf"),
            },
        }
        with patch.object(
            self.manager,
            "_module_state",
            side_effect=[
                {"state": "uninstalled"},
                {"state": "installed", "installed_version": "19.0.1.0.0"},
            ],
        ):
            result = self.manager._execute_step(
                context,
                {
                    "action": "install",
                    "module": "module_a",
                },
            )
        self.assertEqual(result["status"], "success")
        command = self.runner.commands[-1]
        self.assertIn("module", command)
        self.assertIn("install", command)
        self.assertNotIn("shell=True", command)

    def test_odoo17_uninstall_uses_fixed_internal_tool(self):
        context = {"database": "customer", "runtime": {"version": "17"}}
        with (
            patch.object(
                self.manager,
                "_module_state",
                return_value={"state": "installed"},
            ),
            patch.object(
                self.manager,
                "_run_tool",
                return_value={
                    "impacted": [{"name": "module_a"}],
                },
            ) as internal,
        ):
            result = self.manager._execute_step(
                context,
                {
                    "action": "uninstall",
                    "module": "module_a",
                },
            )
        internal.assert_called_once_with(
            context,
            "uninstall",
            ["module_a"],
        )
        self.assertEqual(result["impact"], ["module_a"])

    def test_manifest_scan_ignores_non_literal_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "modulos/module_a"
            bad = root / "modulos/module_b"
            good.mkdir(parents=True)
            bad.mkdir(parents=True)
            (good / "__manifest__.py").write_text(
                "{'name': 'A', 'version': '19.0.1.0.0', "
                "'depends': ['base']}",
                encoding="utf-8",
            )
            (bad / "__manifest__.py").write_text(
                "dict(name='B')",
                encoding="utf-8",
            )

            catalog = self.manager._scan_catalog(
                root,
                ["modulos"],
                "19",
            )

        self.assertIn("module_a", catalog)
        self.assertNotIn("module_b", catalog)

    def test_odoo_module_versions_keep_database_and_disk_separate(self):
        values = _module_values(
            SimpleNamespace(
                name="abc_def",
                state="installed",
                latest_version="19.0.1.1.0",
                installed_version="19.0.1.3.0",
                summary="ABC",
                application=False,
                dependencies_id=[],
            )
        )
        self.assertEqual(values["installed_version"], "19.0.1.1.0")
        self.assertEqual(values["available_version"], "19.0.1.3.0")

    def test_manifest_versions_use_odoo_canonical_format(self):
        cases = {
            "1.1": "19.0.1.1",
            "1.0": "19.0.1.0",
            "0.1": "19.0.0.1",
            "2.0": "19.0.2.0",
            "19.0.1.1": "19.0.1.1",
            "19.0.1.1.0": "19.0.1.1.0",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                canonical = self.manager._adapt_manifest_version(
                    raw,
                    "19",
                )
                self.assertEqual(canonical, expected)
                self.assertEqual(
                    self.manager._version_compare(
                        canonical,
                        expected,
                    ),
                    0,
                )

    def test_manifest_without_version_uses_odoo_default(self):
        version, error = self.manager._catalog_manifest_version(
            {"name": "Module without version"},
            "19",
        )
        self.assertEqual(version, "19.0.1.0")
        self.assertEqual(error, "")

    def test_short_manifest_version_does_not_create_downgrade(self):
        canonical = self.manager._adapt_manifest_version("1.1", "19")
        catalog = {
            "account_accountant": {
                "version": canonical,
                "version_error": "",
                "installable": True,
                "dependencies": [],
            }
        }
        installed = {
            "account_accountant": {
                "state": "installed",
                "installed_version": "19.0.1.1",
            }
        }
        expanded, blockers, _proposed, requested = (
            self.manager._expand_versioned_steps(
                [],
                "update_installed",
                catalog,
                installed,
                [],
                {},
            )
        )
        self.assertEqual(expanded, [])
        self.assertEqual(requested, [])
        self.assertNotIn(
            "anterior a la aplicada",
            "\n".join(blockers),
        )
        self.assertIn(
            "No se requieren actualizaciones",
            "\n".join(blockers),
        )


    def test_runtime_keeps_virtualenv_python_symlink_path(self):
        service = {
            "exec_start": (
                "{ path=/opt/customer/odoo-server/customervenv/bin/python ; "
                "argv[]=/opt/customer/odoo-server/customervenv/bin/python "
                "/opt/customer/odoo-server/odoo-bin "
                "-c /etc/odoocustomer.conf ; }"
            ),
            "config_path": "/etc/odoocustomer.conf",
            "odoo_version": "19",
        }
        with patch.object(Path, "is_file", return_value=True):
            runtime = self.manager._runtime(service)
        self.assertEqual(
            str(runtime["python"]),
            "/opt/customer/odoo-server/customervenv/bin/python",
        )

    def test_runtime_custom_addon_subpaths_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.manager.set_runtime_config(
                {
                    "custom_addons_subpaths": [
                        "custom_addons",
                        "extra/addons",
                    ]
                }
            )
            paths = self.manager.provisioner._custom_addons_paths(
                Path(temporary)
            )
        self.assertEqual(
            paths,
            [
                str(Path(temporary).resolve() / "custom_addons"),
                str(Path(temporary).resolve() / "extra/addons"),
            ],
        )

    def test_inventory_combines_standard_and_managed_modules(self):
        modules = self.manager._combined_inventory(
            {
                "custom_module": {
                    "summary": "Custom",
                    "version": "19.0.2.0.0",
                    "installable": True,
                    "dependencies": ["base"],
                    "subpath": "modulos",
                }
            },
            {
                "base": {
                    "summary": "Base",
                    "state": "installed",
                    "installed_version": "19.0.1.0.0",
                    "dependencies": [],
                },
                "custom_module": {
                    "summary": "Custom",
                    "state": "installed",
                    "installed_version": "19.0.1.0.0",
                    "dependencies": ["base"],
                },
            },
        )
        by_name = {item["name"]: item for item in modules}
        self.assertIn("base", by_name)
        self.assertFalse(by_name["base"]["managed"])
        self.assertTrue(by_name["custom_module"]["managed"])
        self.assertEqual(
            by_name["custom_module"]["database_dependencies"],
            ["base"],
        )


    def test_job_result_retries_while_controller_restarts(self):
        client = ControllerClient(FakeConfig())
        with (
            patch.object(
                client,
                "_post",
                side_effect=[
                    requests.ConnectionError("controller restarting"),
                    {"success": True},
                ],
            ) as post,
            patch("agent.client.time.sleep") as sleep,
        ):
            result = client.job_result(
                42,
                True,
                result={"message": "done"},
            )
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_versioned_route_replays_1_2_and_1_3_in_order(self):
        catalog = {
            "abc_def": {
                "version": "19.0.1.3.0",
                "installable": True,
                "dependencies": ["dependency_x"],
            },
            "dependency_x": {
                "version": "19.0.2.0.0",
                "installable": True,
                "dependencies": [],
            },
        }
        installed = {
            "abc_def": {
                "state": "installed",
                "installed_version": "19.0.1.1.0",
            },
            "dependency_x": {
                "state": "installed",
                "installed_version": "19.0.1.0.0",
            },
        }
        rules = self.manager._validate_migration_rules(
            [
                {
                    "module": "abc_def",
                    "target_version": "19.0.1.2.0",
                    "skip_module_operation": True,
                    "steps": [
                        {
                            "action": "restart",
                            "phase": "after_code",
                        }
                    ],
                },
                {
                    "module": "abc_def",
                    "target_version": "19.0.1.3.0",
                    "steps": [
                        {
                            "action": "upgrade",
                            "module": "dependency_x",
                            "required_version": "19.0.2.0.0",
                            "phase": "after_code",
                        }
                    ],
                },
            ]
        )
        manual = self.manager._validate_steps(
            [
                {
                    "action": "upgrade",
                    "module": "abc_def",
                    "phase": "after_code",
                }
            ]
        )
        expanded, blockers, proposed, _requested = (
            self.manager._expand_versioned_steps(
                manual,
                "manual",
                catalog,
                installed,
                rules,
                {},
            )
        )
        self.assertFalse(blockers)
        self.assertEqual(
            [
                (step["action"], step["module"])
                for step in expanded
            ],
            [
                ("restart", ""),
                ("upgrade", "dependency_x"),
                ("upgrade", "abc_def"),
            ],
        )
        self.assertEqual(
            [
                step.get("transition_version")
                for step in expanded[:2]
            ],
            ["19.0.1.2.0", "19.0.1.3.0"],
        )
        self.assertEqual(proposed["abc_def"], "19.0.1.3.0")

    def test_applied_restart_only_version_skips_old_transition(self):
        catalog = {
            "abc_def": {
                "version": "19.0.1.3.0",
                "installable": True,
                "dependencies": [],
            }
        }
        installed = {
            "abc_def": {
                "state": "installed",
                "installed_version": "19.0.1.1.0",
            }
        }
        rules = self.manager._validate_migration_rules(
            [
                {
                    "module": "abc_def",
                    "target_version": "19.0.1.2.0",
                    "skip_module_operation": True,
                    "steps": [{"action": "restart"}],
                },
                {
                    "module": "abc_def",
                    "target_version": "19.0.1.3.0",
                    "steps": [],
                },
            ]
        )
        manual = self.manager._validate_steps(
            [
                {
                    "action": "upgrade",
                    "module": "abc_def",
                }
            ]
        )
        expanded, blockers, _proposed, _requested = (
            self.manager._expand_versioned_steps(
                manual,
                "manual",
                catalog,
                installed,
                rules,
                {"abc_def": "19.0.1.2.0"},
            )
        )
        self.assertFalse(blockers)
        self.assertEqual(
            [step["action"] for step in expanded],
            ["upgrade"],
        )

    def test_restart_only_latest_records_version_without_upgrade(self):
        catalog = {
            "abc_def": {
                "version": "19.0.1.2.0",
                "installable": True,
                "dependencies": [],
            }
        }
        installed = {
            "abc_def": {
                "state": "installed",
                "installed_version": "19.0.1.1.0",
            }
        }
        rules = self.manager._validate_migration_rules(
            [
                {
                    "module": "abc_def",
                    "target_version": "19.0.1.2.0",
                    "skip_module_operation": True,
                    "steps": [{"action": "restart"}],
                }
            ]
        )
        manual = self.manager._validate_steps(
            [{"action": "upgrade", "module": "abc_def"}]
        )
        expanded, blockers, proposed, _requested = (
            self.manager._expand_versioned_steps(
                manual,
                "manual",
                catalog,
                installed,
                rules,
                {},
            )
        )
        self.assertFalse(blockers)
        self.assertEqual(
            [step["action"] for step in expanded],
            ["restart"],
        )
        self.assertEqual(proposed["abc_def"], "19.0.1.2.0")

    def test_update_installed_selects_only_outdated_installed(self):
        catalog = {
            "old_module": {
                "version": "19.0.2.0.0",
                "installable": True,
                "dependencies": [],
            },
            "current_module": {
                "version": "19.0.1.0.0",
                "installable": True,
                "dependencies": [],
            },
            "new_module": {
                "version": "19.0.1.0.0",
                "installable": True,
                "dependencies": [],
            },
        }
        installed = {
            "old_module": {
                "state": "installed",
                "installed_version": "19.0.1.0.0",
            },
            "current_module": {
                "state": "installed",
                "installed_version": "19.0.1.0.0",
            },
        }
        expanded, blockers, _proposed, requested = (
            self.manager._expand_versioned_steps(
                [],
                "update_installed",
                catalog,
                installed,
                [],
                {},
            )
        )
        self.assertFalse(blockers)
        self.assertEqual(
            [step["module"] for step in requested],
            ["old_module"],
        )
        self.assertEqual(
            [
                (step["action"], step["module"])
                for step in expanded
            ],
            [("upgrade", "old_module")],
        )

    def test_update_installed_selects_module_without_reported_version(self):
        catalog = {
            "module_a": {
                "version": "19.0.2.0.0",
                "installable": True,
                "dependencies": [],
            }
        }
        installed = {
            "module_a": {
                "state": "installed",
                "installed_version": "",
            }
        }

        expanded, blockers, _proposed, requested = (
            self.manager._expand_versioned_steps(
                [],
                "update_installed",
                catalog,
                installed,
                [],
                {},
            )
        )

        self.assertFalse(blockers)
        self.assertEqual([step["module"] for step in requested], ["module_a"])
        self.assertEqual(
            [(step["action"], step["module"]) for step in expanded],
            [("upgrade", "module_a")],
        )
        self.assertEqual(
            expanded[0]["required_version"],
            "19.0.2.0.0",
        )

    def test_upgrade_succeeds_when_odoo_does_not_report_version_afterwards(self):
        context = {
            "database": "customer",
            "runtime": {
                "version": "19",
                "python": Path("/opt/customer/venv/bin/python"),
                "odoo_bin": Path("/opt/customer/odoo-bin"),
                "config": Path("/etc/odoocustomer.conf"),
            },
        }
        with patch.object(
            self.manager,
            "_module_state",
            side_effect=[
                {"state": "installed", "installed_version": ""},
                {"state": "installed", "installed_version": ""},
            ],
        ):
            result = self.manager._execute_step(
                context,
                {
                    "action": "upgrade",
                    "module": "module_a",
                    "required_version": "19.0.2.0.0",
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["installed_version"], "")
        self.assertIn("no informo la version", result["version_warning"])

    def test_verify_without_reported_version_has_clear_error(self):
        with (
            patch.object(
                self.manager,
                "_module_state",
                return_value={"state": "installed", "installed_version": ""},
            ),
            self.assertRaisesRegex(CommandError, "no informa su version"),
        ):
            self.manager._execute_step(
                {"database": "customer", "runtime": {"version": "19"}},
                {
                    "action": "verify",
                    "module": "module_a",
                    "required_version": "19.0.2.0.0",
                },
            )

    def test_dependency_preflight_reports_complete_missing_chain(self):
        context = {
            "service": {"addons_path": "/opt/customer/repo/modulos"},
            "repo": {"path": Path("/opt/customer/repo")},
        }
        catalog = {
            "pw_pos_sale_order": {
                "subpath": "modulos",
                "dependencies": ["l10n_co_pos_dian_ticket"],
            },
            "l10n_co_pos_dian_ticket": {
                "subpath": "modulos",
                "dependencies": ["access_pos_retention_rule"],
            },
            "access_pos_retention_rule": {
                "subpath": "modulos",
                "dependencies": ["partner_declarant_condition"],
            },
        }
        runtime = {
            "modules": [],
            "pending_modules": [],
            "graph_omitted": [],
            "graph_error": "",
        }
        with patch.object(self.manager, "_run_tool", return_value=runtime):
            result = self.manager._dependency_preflight(
                context,
                catalog,
                [{"action": "install", "module": "pw_pos_sale_order"}],
            )

        self.assertIn(
            {
                "chain": [
                    "pw_pos_sale_order",
                    "l10n_co_pos_dian_ticket",
                    "access_pos_retention_rule",
                    "partner_declarant_condition",
                ],
                "reason": "faltante en addons_path o no reconocido por Odoo",
            },
            result["missing_dependencies"],
        )

    def test_dependency_preflight_rejects_git_module_outside_addons_path(self):
        context = {
            "service": {"addons_path": "/opt/customer/other_addons"},
            "repo": {"path": Path("/opt/customer/repo")},
        }
        catalog = {
            "module_a": {"subpath": "modulos", "dependencies": []}
        }
        runtime = {
            "modules": [],
            "pending_modules": [],
            "graph_omitted": [],
            "graph_error": "",
        }
        with patch.object(self.manager, "_run_tool", return_value=runtime):
            result = self.manager._dependency_preflight(
                context,
                catalog,
                [{"action": "install", "module": "module_a"}],
            )

        self.assertIn("existe en Git", result["blockers"][0])

    def test_install_requires_installed_state_after_command(self):
        context = {
            "database": "customer",
            "runtime": {
                "version": "19",
                "python": Path("/opt/customer/venv/bin/python"),
                "odoo_bin": Path("/opt/customer/odoo-bin"),
                "config": Path("/etc/odoocustomer.conf"),
            },
        }
        with (
            patch.object(
                self.manager,
                "_module_state",
                side_effect=[
                    {"state": "uninstalled"},
                    {"state": "uninstalled"},
                ],
            ),
            self.assertRaisesRegex(CommandError, "no en installed"),
        ):
            self.manager._execute_step(
                context,
                {"action": "install", "module": "module_a"},
            )

if __name__ == "__main__":
    unittest.main()
