import json
import os
import re
import tempfile
from pathlib import Path

from .commands import CommandError, run, systemd_status
from .discovery import OdooServiceDiscovery
from .module_manager import OdooModuleManager


DATABASE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
MODULE_NAME = "account_invoicing_limit"
RESULT_PREFIX = "GENESIS_LICENSE_RESULT="


class OdooLicenseManager:
    """Bootstrap the fixed client licensing module on an associated database."""

    def __init__(
        self,
        config,
        progress_callback=None,
        runner=None,
        discovery=None,
        status_reader=None,
    ):
        self.config = config
        self.progress_callback = progress_callback
        self.runner = runner or run
        self.discovery = discovery or OdooServiceDiscovery(config)
        self.status_reader = status_reader or systemd_status
        self.tool_path = Path(__file__).with_name("odoo_license_tool.py")

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

    @staticmethod
    def _database_name(value):
        name = str(value or "").strip()
        if not DATABASE_RE.fullmatch(name):
            raise CommandError("Nombre de base de datos invalido.")
        return name

    def _context(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        module_name = str(payload.get("module_name") or MODULE_NAME).strip()
        if module_name != MODULE_NAME:
            raise CommandError("El trabajo solo admite account_invoicing_limit.")
        unit = str(payload.get("service_name") or "").strip()
        if not unit:
            raise CommandError("Falta la unidad Odoo.")
        database = self._database_name(payload.get("database_name"))

        discovered = self.discovery.discover()
        services = discovered.get("services") or []
        service = next(
            (item for item in services if item.get("unit") == unit),
            None,
        )
        if not service:
            raise CommandError("El servicio Odoo no fue descubierto.")
        if not service.get("control_allowed"):
            raise CommandError("El servicio Odoo no esta permitido para control.")

        inventory = self.discovery.discover_databases(services)
        database_item = next(
            (
                item
                for item in (inventory.get("databases") or [])
                if item.get("name") == database
                and item.get("service_unit") == unit
                and item.get("association_state") == "matched"
            ),
            None,
        )
        if not database_item:
            raise CommandError(
                "La base no esta asociada de forma inequivoca al servicio."
            )

        return {
            "service": service,
            "database": database,
            "database_item": database_item,
            "runtime": OdooModuleManager._runtime(service),
        }

    def _run_tool(self, context, mode):
        descriptor, name = tempfile.mkstemp(
            prefix="genesis-license-request-",
            suffix=".json",
        )
        request_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"mode": mode}, handle)
            os.chmod(request_path, 0o644)
            runtime = context["runtime"]
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(runtime["python"]),
                str(self.tool_path),
                "--odoo-root",
                str(runtime["odoo_root"]),
                "--config",
                str(runtime["config"]),
                "--database",
                context["database"],
                "--request",
                str(request_path),
            ]
            result = self.runner(command, check=False, timeout=1800)
            output = str(result.get("output") or "")
            if not result.get("success"):
                raise CommandError(
                    "Fallo la preparacion interna de licencias. "
                    "Revise el log local del servicio Odoo."
                )
            line = next(
                (
                    value[len(RESULT_PREFIX):]
                    for value in reversed(output.splitlines())
                    if value.startswith(RESULT_PREFIX)
                ),
                "",
            )
            if not line:
                raise CommandError("La utilidad de licencias no devolvio resultado.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CommandError("Resultado de licencias invalido.")
            return value
        finally:
            request_path.unlink(missing_ok=True)

    def _install_module(self, context):
        runtime = context["runtime"]
        if str(runtime["version"] or "").split(".", 1)[0] == "19":
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(runtime["python"]),
                str(runtime["odoo_bin"]),
                "module",
                "install",
                "-c",
                str(runtime["config"]),
                "-d",
                context["database"],
                MODULE_NAME,
            ]
        else:
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(runtime["python"]),
                str(runtime["odoo_bin"]),
                "-c",
                str(runtime["config"]),
                "-d",
                context["database"],
                "-i",
                MODULE_NAME,
                "--stop-after-init",
                "--no-http",
            ]
        result = self.runner(command, check=False, timeout=3600)
        if not result.get("success"):
            detail = str(result.get("output") or "").strip()[-2500:]
            raise CommandError(
                f"Fallo la instalacion de {MODULE_NAME}."
                + (f" Detalle: {detail}" if detail else "")
            )

    def bootstrap(self, payload):
        self._progress("discover", 10, "Validando servicio y base de datos.")
        context = self._context(payload)
        unit = context["service"]["unit"]

        self._progress("module", 25, "Verificando account_invoicing_limit.")
        status = self._run_tool(context, "status")
        installed_before = bool(status.get("module_installed"))
        was_running = self.status_reader(unit).get("active_state") == "active"
        stopped = False
        failure = None
        result = None
        try:
            if not installed_before:
                if was_running:
                    self._progress("stop", 35, "Deteniendo temporalmente Odoo.")
                    stop_result = self.runner(
                        ["systemctl", "stop", unit],
                        check=False,
                        timeout=120,
                    )
                    if not stop_result.get("success"):
                        raise CommandError("No fue posible detener el servicio Odoo.")
                    stopped = True

                self._progress("install", 55, "Instalando account_invoicing_limit.")
                self._install_module(context)

            self._progress("credential", 78, "Preparando credencial de licencias.")
            result = self._run_tool(context, "bootstrap")
        except Exception as exc:  # pylint: disable=broad-except
            failure = exc
        finally:
            if was_running and stopped:
                self._progress("start", 92, "Iniciando nuevamente Odoo.")
                start_result = self.runner(
                    ["systemctl", "start", unit],
                    check=False,
                    timeout=180,
                )
                if not start_result.get("success") and failure is None:
                    failure = CommandError(
                        "La licencia se preparo, pero el servicio Odoo no inicio."
                    )

        if failure is not None:
            raise failure

        self._progress("finished", 100, "Licencias preparadas correctamente.")
        return {
            **(result or {}),
            "success": True,
            "installed_by_job": not installed_before,
            "message": (
                "account_invoicing_limit y su credencial quedaron preparados."
            ),
        }
