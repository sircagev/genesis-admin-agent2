import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.client import ControllerClient
from agent.database_manager import DatabaseManager
from agent.discovery import OdooServiceDiscovery
from agent.executor import JobExecutor
from agent.commands import CommandError
from agent.provisioner import OdooProvisioner


class DatabaseDiscoveryAssociationTest(unittest.TestCase):
    def _discover(self, rows, services):
        discovery = OdooServiceDiscovery.__new__(OdooServiceDiscovery)

        with patch(
            "agent.discovery.run",
            return_value={"success": True, "output": rows},
        ):
            return discovery.discover_databases(services=services)["databases"]

    @staticmethod
    def _row(name, owner):
        return f"{name}\t{owner}\t1048576\tUTF8\tC\tC"

    def test_db_name_has_priority_over_dbfilter_and_owner(self):
        services = [
            {
                "unit": "odoo-server-name.service",
                "technical_name": "name",
                "db_name": "customer",
                "db_user": "shared",
            },
            {
                "unit": "odoo-server-filter.service",
                "technical_name": "filter",
                "dbfilter": "^customer$",
            },
            {
                "unit": "odoo-server-owner.service",
                "technical_name": "owner",
                "db_user": "shared",
            },
        ]

        database = self._discover(
            self._row("customer", "shared"),
            services,
        )[0]

        self.assertEqual(database["service_technical_name"], "name")
        self.assertEqual(database["association_method"], "db_name")
        self.assertEqual(database["association_state"], "matched")

    def test_dbfilter_has_priority_over_owner(self):
        services = [
            {
                "unit": "odoo-server-filter.service",
                "technical_name": "filter",
                "dbfilter": "^customer$",
            },
            {
                "unit": "odoo-server-owner.service",
                "technical_name": "owner",
                "db_user": "shared",
            },
        ]

        database = self._discover(
            self._row("customer", "shared"),
            services,
        )[0]

        self.assertEqual(database["service_technical_name"], "filter")
        self.assertEqual(database["association_method"], "dbfilter")
        self.assertEqual(database["association_state"], "matched")

    def test_dbfilter_pattern_links_multiple_databases(self):
        services = [
            {
                "unit": "odoo-server-customer.service",
                "technical_name": "customer",
                "dbfilter": "^customer.*",
            }
        ]

        databases = self._discover(
            "\n".join(
                [
                    self._row("customer", "customer"),
                    self._row("customer_test", "customer"),
                    self._row("customer_demo", "customer"),
                ]
            ),
            services,
        )

        self.assertEqual(len(databases), 3)
        self.assertTrue(
            all(
                item["service_technical_name"] == "customer"
                and item["association_method"] == "dbfilter"
                and item["association_state"] == "matched"
                for item in databases
            )
        )

    def test_owner_with_one_service_links_all_owned_databases(self):
        services = [
            {
                "unit": "odoo-server-owner.service",
                "technical_name": "owner",
                "db_user": "shared",
            }
        ]

        databases = self._discover(
            "\n".join(
                [
                    self._row("customer", "shared"),
                    self._row("other", "shared"),
                ]
            ),
            services,
        )

        self.assertTrue(
            all(
                item["service_technical_name"] == "owner"
                and item["association_method"] == "owner"
                and item["association_state"] == "matched"
                for item in databases
            )
        )

    def test_owner_with_multiple_services_is_ambiguous(self):
        services = [
            {
                "unit": "odoo-server-one.service",
                "technical_name": "one",
                "db_user": "shared",
            },
            {
                "unit": "odoo-server-two.service",
                "technical_name": "two",
                "db_user": "shared",
            },
        ]

        database = self._discover(
            self._row("customer", "shared"),
            services,
        )[0]

        self.assertFalse(database["service_technical_name"])
        self.assertEqual(database["association_state"], "ambiguous")
        self.assertIn("varios servicios", database["association_message"])

    def test_ambiguous_exact_name_does_not_fall_back(self):
        services = [
            {
                "unit": "odoo-server-one.service",
                "technical_name": "one",
                "db_name": "customer",
            },
            {
                "unit": "odoo-server-two.service",
                "technical_name": "two",
                "db_name": "customer",
            },
            {
                "unit": "odoo-server-owner.service",
                "technical_name": "owner",
                "db_user": "shared",
            },
        ]

        database = self._discover(
            self._row("customer", "shared"),
            services,
        )[0]

        self.assertFalse(database["service_technical_name"])
        self.assertEqual(database["association_state"], "ambiguous")


class InventoryProgressTest(unittest.TestCase):
    @staticmethod
    def _executor(discovery):
        executor = JobExecutor.__new__(JobExecutor)
        executor.discovery = discovery
        return executor

    def test_services_inventory_reports_modal_stages(self):
        class Discovery:
            @staticmethod
            def discover():
                return {"success": True, "services": [], "count": 0}

        executor = self._executor(Discovery())
        progress = []
        executor.set_progress_callback(
            lambda stage, percent, message: progress.append(
                (stage, percent, message)
            )
        )

        result = executor.inventory_services({})

        self.assertTrue(result["success"])
        self.assertEqual(
            [item[0] for item in progress],
            ["agent", "systemd", "configs", "nginx", "synchronizing"],
        )

    def test_database_inventory_reports_modal_stages(self):
        class Discovery:
            @staticmethod
            def discover():
                return {"success": True, "services": [], "count": 0}

            @staticmethod
            def discover_databases(services=None):
                return {"success": True, "databases": [], "count": 0}

        executor = self._executor(Discovery())
        progress = []
        executor.set_progress_callback(
            lambda stage, percent, message: progress.append(
                (stage, percent, message)
            )
        )

        result = executor.inventory_databases({})

        self.assertTrue(result["success"])
        self.assertEqual(
            [item[0] for item in progress],
            [
                "agent",
                "postgresql",
                "detecting",
                "owner",
                "configs",
                "associating",
                "updating",
            ],
        )


class DatabaseRestoreStreamTest(unittest.TestCase):
    def test_pg_restore_reads_private_dump_through_stdin(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["stdin_data"] = kwargs["stdin"].read()
            return subprocess.CompletedProcess(command, 0, stdout=b"")

        with tempfile.TemporaryDirectory() as directory:
            dump_path = Path(directory) / "database.dump"
            dump_path.write_bytes(b"private-dump")

            with patch("agent.database_manager.subprocess.run", fake_run):
                DatabaseManager._pg_restore_from_file(
                    dump_path,
                    "customer",
                    "customer",
                )

        self.assertEqual(captured["stdin_data"], b"private-dump")
        self.assertEqual(captured["command"][:5], [
            "runuser",
            "-u",
            "postgres",
            "--",
            "pg_restore",
        ])
        self.assertNotIn(str(dump_path), captured["command"])

    def test_controller_client_keeps_job_and_transfer_contracts(self):
        for method_name in (
            "provisioning_config",
            "job_progress",
            "transfer_begin",
            "transfer_upload",
            "transfer_complete",
            "transfer_download",
        ):
            self.assertTrue(callable(getattr(ControllerClient, method_name)))


class ProvisioningContractTest(unittest.TestCase):
    class Config:
        def __init__(self, provision=None):
            self.provision = provision or {}

        def section(self, name):
            return dict(self.provision) if name == "provision" else {}

        def get(self, _key, default=None):
            return default

    @staticmethod
    def _payload():
        return {
            "version_odoo": "19",
            "http_port": 8069,
            "gevent_port": 8070,
            "workers": 2,
            "max_cron_threads": 1,
            "proxy_mode": True,
            "log_level": "warn",
            "database_name": "customer",
        }

    def _provisioner(self, **values):
        defaults = {
            "postgres_host": "127.0.0.1",
            "postgres_port": 5432,
            "postgres_default_password": "one-source-secret",
            "postgres_validate_auth": True,
            "admin_passwd": "master-secret",
        }
        defaults.update(values)
        return OdooProvisioner(self.Config(defaults))

    def test_config_contains_tcp_password_and_exact_addons(self):
        provisioner = self._provisioner()
        addons = ["/opt/customer/odoo-server/addons", "/opt/customer/odoo-server/modulosFE19/custom_addons", "/opt/customer/odoo-server/modulosFE19/modulos"]
        config = provisioner._odoo_conf(
            self._payload(), "customer", addons, Path("/var/log/odoo/customer.log")
        )
        self.assertIn("db_host = 127.0.0.1", config)
        self.assertIn("db_port = 5432", config)
        self.assertIn("db_password = one-source-secret", config)
        self.assertIn("addons_path = " + ",".join(addons), config)

    def test_role_and_config_use_same_password_source(self):
        provisioner = self._provisioner()
        with patch.object(
            provisioner, "_run_postgres_sql_private", return_value=True
        ) as mocked:
            provisioner._ensure_postgres_role("customer")
        sql = mocked.call_args.args[0]
        config = provisioner._odoo_conf(
            self._payload(), "customer", ["/addons"], Path("/tmp/odoo.log")
        )
        self.assertIn("one-source-secret", sql)
        self.assertIn("db_password = one-source-secret", config)
        self.assertIn("CREATEROLE", sql)

    def test_role_password_is_sent_by_stdin_not_command(self):
        provisioner = self._provisioner()
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)
            captured["input"] = kwargs.get("input", "")
            return subprocess.CompletedProcess(command, 0, stdout="")

        with patch("agent.provisioner.subprocess.run", fake_run):
            provisioner._ensure_postgres_role("customer")
        self.assertNotIn("one-source-secret", " ".join(captured["command"]))
        self.assertIn("one-source-secret", captured["input"])

    def test_tcp_auth_uses_environment_not_command(self):
        provisioner = self._provisioner()
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(command=list(command), env=dict(kwargs.get("env", {})))
            return {"success": True, "output": "1"}

        with patch("agent.provisioner.run", fake_run):
            self.assertTrue(provisioner._validate_postgres_auth("customer"))
        self.assertEqual(captured["env"]["PGPASSWORD"], "one-source-secret")
        self.assertNotIn("one-source-secret", " ".join(captured["command"]))

    def test_tcp_auth_failure_is_not_silent(self):
        provisioner = self._provisioner()
        with patch("agent.provisioner.run", return_value={"success": False, "output": "denied"}):
            with self.assertRaisesRegex(CommandError, "No fue posible autenticar"):
                provisioner._validate_postgres_auth("customer")

    def test_restore_repairs_legacy_database_connection_config(self):
        provisioner = self._provisioner(postgres_default_password="legacy")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "odoo.conf"
            path.write_text(
                provisioner._odoo_conf(
                    self._payload(),
                    "customer",
                    ["/opt/customer/odoo-server/addons"],
                    Path("/tmp/customer.log"),
                ).replace("db_host = 127.0.0.1", "db_host = False")
                .replace("db_password = legacy", "db_password = False"),
                encoding="utf-8",
            )
            provisioner.set_runtime_config(
                {
                    "postgres_host": "127.0.0.1",
                    "postgres_port": 5432,
                    "postgres_default_password": "repaired-secret",
                }
            )
            provisioner._ensure_database_connection_config(path, "customer")
            repaired = provisioner._read_odoo_config(path)
        self.assertEqual(repaired["db_host"], "127.0.0.1")
        self.assertEqual(repaired["db_port"], "5432")
        self.assertEqual(repaired["db_user"], "customer")
        self.assertEqual(repaired["db_password"], "repaired-secret")

    def test_odoo_config_validation_rejects_missing_password(self):
        provisioner = self._provisioner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "odoo.conf"
            path.write_text("[options]\ndb_host=127.0.0.1\n", encoding="utf-8")
            with self.assertRaisesRegex(CommandError, "incompleta"):
                provisioner._validate_odoo_conf(path, self._payload())

    def test_fresh_odoo_clone_removes_only_upstream_metadata(self):
        provisioner = self._provisioner()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "customer"
            base.mkdir()
            target = base / "odoo-server"

            def fake_git(_cmd, _repo, **_kwargs):
                target.mkdir()
                (target / "odoo-bin").write_text("", encoding="utf-8")
                (target / ".git").mkdir()
                (target / ".github").mkdir()
                (target / "addons").mkdir()
                return {"success": True, "output": ""}

            with patch.object(provisioner, "_run_git", fake_git):
                self.assertTrue(provisioner._prepare_odoo_tree(target, base, "https://github.com/odoo/odoo.git", "19.0"))
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / ".github").exists())
            self.assertTrue((target / "addons").exists())

    def test_existing_valid_application_tree_is_reused(self):
        provisioner = self._provisioner()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "customer"
            target = base / "odoo-server"
            target.mkdir(parents=True)
            (target / "odoo-bin").write_text("", encoding="utf-8")
            with patch.object(provisioner, "_run_git") as mocked:
                self.assertFalse(provisioner._prepare_odoo_tree(target, base, "repo", "19.0"))
            mocked.assert_not_called()

    def test_custom_fresh_target_validates_access_and_clones_exact_repo(self):
        provisioner = self._provisioner(custom_addons_branch="19.0")
        repo = "https://github.com/acme/private.git"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "modulosFE19"
            with patch.object(
                provisioner,
                "_run_git",
                return_value={"success": True},
            ) as mocked:
                result = provisioner._prepare_custom_repo(repo, target)
        self.assertIn("clonado", result)
        self.assertEqual(mocked.call_count, 2)
        preflight = mocked.call_args_list[0]
        clone = mocked.call_args_list[1]
        self.assertIn("ls-remote", preflight.args[0])
        self.assertEqual(preflight.args[0][-2:], [repo, "HEAD"])
        self.assertEqual(preflight.args[1], repo)
        self.assertIn("clone", clone.args[0])
        self.assertIn(repo, clone.args[0])
        self.assertEqual(clone.args[1], repo)

    def test_existing_custom_repo_fetches_without_clone(self):
        provisioner = self._provisioner(custom_addons_branch="19.0")
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "modulosFE19"
            (target / ".git").mkdir(parents=True)

            def fake_git(command, _repo, **kwargs):
                calls.append(command)
                if "get-url" in command:
                    return {"success": True, "output": "https://github.com/acme/private.git"}
                return {"success": True, "output": ""}

            with patch.object(provisioner, "_run_git", fake_git):
                provisioner._prepare_custom_repo("https://github.com/acme/private.git", target)
        self.assertFalse(any("clone" in command for command in calls))
        self.assertTrue(any("fetch" in command for command in calls))
        self.assertTrue(any("--ff-only" in command for command in calls))

    def test_custom_remote_mismatch_fails_safely(self):
        provisioner = self._provisioner()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "modulosFE19"
            (target / ".git").mkdir(parents=True)
            with patch.object(provisioner, "_run_git", return_value={"success": True, "output": "https://github.com/other/repo.git"}):
                with self.assertRaisesRegex(CommandError, "otro repositorio"):
                    provisioner._prepare_custom_repo("https://github.com/acme/private.git", target)

    def test_custom_non_git_preexisting_target_is_preserved(self):
        provisioner = self._provisioner()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "modulosFE19"
            target.mkdir()
            sentinel = target / "user-file"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(CommandError, "no será reemplazado"):
                provisioner._prepare_custom_repo("https://github.com/acme/private.git", target)
            self.assertTrue(sentinel.exists())

    def test_git_token_is_temporary_and_not_in_command_or_script(self):
        token = "github-test-token-never-log"
        provisioner = self._provisioner(
            github_auth_enabled=True,
            github_token=token,
            github_username="octocat",
        )
        captured = {}

        def fake_run(command, **kwargs):
            env = kwargs["env"]
            captured["command"] = command
            captured["username"] = env.get("GITHUB_USERNAME")
            captured["token"] = env.get("GITHUB_TOKEN")
            captured["script"] = Path(env["GIT_ASKPASS"]).read_text(encoding="utf-8")
            return {"success": True, "output": ""}

        command = ["git", "clone", "https://github.com/acme/private.git", "/tmp/target"]
        with patch("agent.provisioner.run", fake_run):
            provisioner._run_git(command, "https://github.com/acme/private.git")
        self.assertEqual(captured["username"], "octocat")
        self.assertEqual(captured["token"], token)
        self.assertNotIn(token, " ".join(captured["command"]))
        self.assertNotIn(token, captured["script"])
        self.assertIn("$GITHUB_USERNAME", captured["script"])
        self.assertIn("$GITHUB_TOKEN", captured["script"])

    def test_runtime_github_config_reaches_git_environment(self):
        token = "github_runtime_token_1234567890"
        repo = "https://github.com/evopos30/modulosFE19.git"
        provisioner = self._provisioner()
        provisioner.set_runtime_config(
            {
                "custom_addons_repo": repo,
                "custom_addons_branch": "19.0",
                "github_auth_enabled": True,
                "github_username": "evopos30",
                "github_token": token,
            }
        )
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["username"] = kwargs["env"].get("GITHUB_USERNAME")
            captured["token"] = kwargs["env"].get("GITHUB_TOKEN")
            return {"success": True, "output": "abc\tHEAD"}

        with patch("agent.provisioner.run", fake_run):
            provisioner._validate_git_access(repo)

        self.assertEqual(provisioner.pcfg["custom_addons_repo"], repo)
        self.assertEqual(provisioner.pcfg["custom_addons_branch"], "19.0")
        self.assertTrue(provisioner.pcfg["github_auth_enabled"])
        self.assertEqual(captured["username"], "evopos30")
        self.assertEqual(captured["token"], token)
        self.assertEqual(captured["command"][-2:], [repo, "HEAD"])

    def test_missing_username_uses_github_repo_owner(self):
        provisioner = self._provisioner(
            github_auth_enabled=True,
            github_token="github_owner_token_1234567890",
            github_username="",
        )
        env, directory, _token = provisioner._git_environment(
            "https://github.com/evopos30/modulosFE19.git"
        )
        try:
            self.assertEqual(env["GITHUB_USERNAME"], "evopos30")
        finally:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)

    def test_malformed_github_token_is_rejected_before_git(self):
        provisioner = self._provisioner(
            github_auth_enabled=True,
            github_token="github_token_1234567890@example.com",
        )
        with self.assertRaisesRegex(CommandError, "únicamente el PAT"):
            provisioner._validate_git_access(
                "https://github.com/acme/private.git"
            )

    def test_git_auth_error_redacts_token_and_uses_actionable_message(self):
        token = "github_redaction_token_1234567890"
        provisioner = self._provisioner(
            github_auth_enabled=True,
            github_token=token,
            github_username="octocat",
        )

        def fake_run(_command, **_kwargs):
            raise CommandError(
                f"remote: Invalid username or token: {token}\n"
                "fatal: Authentication failed"
            )

        with patch("agent.provisioner.run", fake_run):
            with self.assertRaises(CommandError) as caught:
                provisioner._validate_git_access(
                    "https://github.com/acme/private.git"
                )
        message = str(caught.exception)
        self.assertNotIn(token, message)
        self.assertIn("GitHub rechazó la autenticación", message)
        self.assertIn("Verifique usuario, token y permisos", message)

    def test_enabled_github_auth_requires_token(self):
        provisioner = self._provisioner(
            github_auth_enabled=True,
            github_token="",
        )
        with self.assertRaisesRegex(CommandError, "token no está configurado"):
            provisioner._git_environment("https://github.com/acme/private.git")

    def test_runtime_config_replaces_defaults_in_memory(self):
        provisioner = self._provisioner()
        provisioner.set_runtime_config({"postgres_default_password": "runtime-secret", "unknown": "ignored"})
        self.assertEqual(provisioner.pcfg["postgres_default_password"], "runtime-secret")
        self.assertNotIn("unknown", provisioner.pcfg)

    def test_tree_stats_counts_each_file_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").write_bytes(b"1")
            (root / "two").write_bytes(b"22")
            stats = DatabaseManager._tree_stats(root)
        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["size"], 3)


if __name__ == "__main__":
    unittest.main()
