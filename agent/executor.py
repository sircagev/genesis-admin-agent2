from .commands import (
    CommandError,
    assert_unit_name,
    run,
    systemd_status,
)
from .provisioner import OdooProvisioner
from .discovery import OdooServiceDiscovery


class JobExecutor:
    def __init__(self, config):
        self.config = config
        self.provisioner = OdooProvisioner(config)
        self.discovery = OdooServiceDiscovery(config)
        self.allowed_exact = set(config.get("allowed_exact") or [])
        self.allowed_prefix = tuple(config.get("allowed_prefix") or [])
        self.default_log_lines = int(config.get("log_default_lines") or 200)

    def execute(self, job):
        job_type = job.get("job_type")
        payload = job.get("payload") or {}

        handlers = {
            "service.status": self.service_status,
            "service.start": self.service_start,
            "service.stop": self.service_stop,
            "service.restart": self.service_restart,
            "service.logs": self.service_logs,
            "inventory.services": self.inventory_services,
            "provision.prepare": self.provision_prepare,
            "provision.adopt": self.provision_adopt,
            "provision.create": self.provision_create,
        }

        handler = handlers.get(job_type)

        if not handler:
            raise CommandError(
                f"Tipo de trabajo no soportado: {job_type}"
            )

        return handler(payload)

    def _allowed_unit(self, payload):
        unit = assert_unit_name(
            payload.get("service_name")
        )

        if unit in self.allowed_exact:
            return unit

        if self.allowed_prefix and unit.startswith(
            self.allowed_prefix
        ):
            return unit

        raise CommandError(
            f"Unidad no permitida por política: {unit}"
        )

    def service_status(self, payload):
        unit = self._allowed_unit(payload)

        return {
            "success": True,
            "status": systemd_status(unit),
        }

    def _systemctl(self, action, payload):
        unit = self._allowed_unit(payload)

        result = run(
            [
                "systemctl",
                action,
                unit,
            ],
            timeout=120,
        )

        return {
            "success": True,
            "service_name": unit,
            "action": action,
            "output": result["output"],
            "status": systemd_status(unit),
        }

    def service_start(self, payload):
        return self._systemctl(
            "start",
            payload,
        )

    def service_stop(self, payload):
        return self._systemctl(
            "stop",
            payload,
        )

    def service_restart(self, payload):
        return self._systemctl(
            "restart",
            payload,
        )

    def service_logs(self, payload):
        unit = self._allowed_unit(payload)

        lines = min(
            max(
                int(
                    payload.get("lines")
                    or self.default_log_lines
                ),
                1,
            ),
            2000,
        )

        result = run(
            [
                "journalctl",
                "-u",
                unit,
                "-n",
                str(lines),
                "--no-pager",
            ],
            timeout=30,
            check=False,
        )

        return {
            "success": True,
            "service_name": unit,
            "lines": lines,
            "logs": result["output"],
        }

    def provision_prepare(self, payload):
        return self.provisioner.prepare(payload)

    def provision_adopt(self, payload):
        return self.provisioner.adopt(payload)

    def provision_create(self, payload):
        return self.provisioner.create(payload)

    def inventory_services(self, _payload):
        return self.discovery.discover()