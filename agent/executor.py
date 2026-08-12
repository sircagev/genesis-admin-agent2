from .commands import (
    CommandError,
    assert_unit_name,
    run,
    systemd_status,
)
from .discovery import OdooServiceDiscovery
from .provisioner import OdooProvisioner


class JobExecutor:
    def __init__(self, config):
        self.config = config

        self.provisioner = OdooProvisioner(
            config
        )

        self.discovery = OdooServiceDiscovery(
            config
        )

        self.allowed_exact = set(
            config.get("allowed_exact")
            or []
        )

        self.allowed_prefix = tuple(
            config.get("allowed_prefix")
            or []
        )

        self.default_log_lines = int(
            config.get("log_default_lines")
            or 200
        )

    def execute(self, job):
        job_type = job.get("job_type")
        payload = job.get("payload") or {}

        handlers = {
            "service.status":
                self.service_status,

            "service.start":
                self.service_start,

            "service.stop":
                self.service_stop,

            "service.restart":
                self.service_restart,

            "service.logs":
                self.service_logs,

            "inventory.services":
                self.inventory_services,

            "inventory.ports":
                self.inventory_ports,

            "provision.prepare":
                self.provision_prepare,

            "provision.adopt":
                self.provision_adopt,

            "provision.create":
                self.provision_create,

            "provision.auto_create":
                self.provision_auto_create,

            "provision.finalize":
                self.provision_finalize,
        }

        handler = handlers.get(
            job_type
        )

        if not handler:
            raise CommandError(
                "Tipo de trabajo no soportado: "
                f"{job_type}"
            )

        return handler(
            payload
        )

    # ---------------------------------------------------------
    # VALIDAR UNIDAD PERMITIDA
    # ---------------------------------------------------------

    def _allowed_unit(self, payload):
        unit = assert_unit_name(
            payload.get(
                "service_name"
            )
        )

        if unit in self.allowed_exact:
            return unit

        if (
            self.allowed_prefix
            and unit.startswith(
                self.allowed_prefix
            )
        ):
            return unit

        raise CommandError(
            "Unidad no permitida "
            f"por política: {unit}"
        )

    # ---------------------------------------------------------
    # ESTADO
    # ---------------------------------------------------------

    def service_status(self, payload):
        unit = self._allowed_unit(
            payload
        )

        return {
            "success": True,
            "status": systemd_status(
                unit
            ),
        }

    # ---------------------------------------------------------
    # SYSTEMCTL
    # ---------------------------------------------------------

    def _systemctl(
        self,
        action,
        payload,
    ):
        unit = self._allowed_unit(
            payload
        )

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

            "output": result[
                "output"
            ],

            "status": systemd_status(
                unit
            ),
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

    # ---------------------------------------------------------
    # LOGS
    # ---------------------------------------------------------

    def service_logs(self, payload):
        unit = self._allowed_unit(
            payload
        )

        lines = min(
            max(
                int(
                    payload.get(
                        "lines"
                    )
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

            "service_name":
                unit,

            "lines":
                lines,

            "logs":
                result["output"],
        }

    # ---------------------------------------------------------
    # INVENTARIO
    # ---------------------------------------------------------

    def inventory_services(
        self,
        _payload,
    ):
        return self.discovery.discover()

    # ---------------------------------------------------------
    # PUERTOS
    # ---------------------------------------------------------

    def inventory_ports(
        self,
        payload,
    ):
        return (
            self.discovery
            .discover_ports(
                start_port=int(
                    payload.get(
                        "start_port"
                    )
                    or 8069
                ),

                end_port=int(
                    payload.get(
                        "end_port"
                    )
                    or 9000
                ),

                pair_limit=int(
                    payload.get(
                        "pair_limit"
                    )
                    or 30
                ),
            )
        )

    # ---------------------------------------------------------
    # APROVISIONAMIENTO EXISTENTE
    # ---------------------------------------------------------

    def provision_prepare(
        self,
        payload,
    ):
        return self.provisioner.prepare(
            payload
        )

    def provision_adopt(
        self,
        payload,
    ):
        return self.provisioner.adopt(
            payload
        )

    def provision_create(
        self,
        payload,
    ):
        return self.provisioner.create(
            payload
        )

    # ---------------------------------------------------------
    # CREACIÓN AUTOMÁTICA COMPLETA
    # ---------------------------------------------------------

    def provision_auto_create(
        self,
        payload,
    ):
        """
        Busca el par de puertos justo antes de crear.

        No confiamos en una consulta de puertos hecha
        anteriormente desde la interfaz.
        """

        self._allowed_unit(
            payload
        )

        # -----------------------------------------------------
        # 1. BUSCAR PUERTOS
        # -----------------------------------------------------

        port_result = (
            self.discovery
            .discover_ports(
                start_port=int(
                    payload.get(
                        "port_start"
                    )
                    or 8069
                ),

                end_port=int(
                    payload.get(
                        "port_end"
                    )
                    or 9000
                ),

                pair_limit=1,
            )
        )

        pair = (
            port_result.get(
                "recommended_pair"
            )
            or {}
        )

        if not pair:
            raise CommandError(
                "No existe un par consecutivo "
                "de puertos disponible en el "
                "rango solicitado."
            )

        # -----------------------------------------------------
        # 2. PAYLOAD DEFINITIVO
        # -----------------------------------------------------

        create_payload = {
            **payload,

            "http_port":
                int(
                    pair[
                        "http_port"
                    ]
                ),

            "gevent_port":
                int(
                    pair[
                        "gevent_port"
                    ]
                ),

            # SSL NO se hace todavía.
            #
            # Primero creamos:
            # Odoo
            # systemd
            # Nginx
            #
            # Luego verificamos DNS público.
            "create_ssl":
                False,

            "dry_run":
                False,
        }

        # -----------------------------------------------------
        # 3. CREAR INSTANCIA
        # -----------------------------------------------------

        created = (
            self.provisioner
            .create(
                create_payload
            )
        )

        created[
            "assigned_ports"
        ] = {
            "http_port":
                create_payload[
                    "http_port"
                ],

            "gevent_port":
                create_payload[
                    "gevent_port"
                ],
        }

        created[
            "port_scan"
        ] = {
            "scan_start":
                port_result.get(
                    "scan_start"
                ),

            "scan_end":
                port_result.get(
                    "scan_end"
                ),
        }

        if not created.get(
            "success"
        ):
            return created

        # -----------------------------------------------------
        # 4. PREPARAR FINALIZACIÓN
        # -----------------------------------------------------

        finalize_payload = {
            **create_payload,

            "create_ssl":
                bool(
                    payload.get(
                        "create_ssl",
                        True,
                    )
                ),

            "certbot_email":
                payload.get(
                    "certbot_email"
                )
                or "",

            "expected_ipv4":
                payload.get(
                    "expected_ipv4"
                )
                or "",
        }

        # -----------------------------------------------------
        # 5. DNS + SSL + HEALTH
        # -----------------------------------------------------

        finalized = (
            self.provisioner
            .finalize(
                finalize_payload
            )
        )

        created[
            "finalize"
        ] = finalized

        created[
            "waiting_dns"
        ] = bool(
            finalized.get(
                "waiting_dns"
            )
        )

        created[
            "health"
        ] = (
            finalized.get(
                "health"
            )
            or {}
        )

        created[
            "dns_addresses"
        ] = (
            finalized.get(
                "dns_addresses"
            )
            or []
        )

        created[
            "status"
        ] = (
            finalized.get(
                "status"
            )
            or {}
        )

        # -----------------------------------------------------
        # DNS TODAVÍA NO PROPAGÓ
        # -----------------------------------------------------

        if finalized.get(
            "waiting_dns"
        ):
            created[
                "message"
            ] = finalized.get(
                "message"
            )

            # Importante:
            #
            # La instancia sí se creó correctamente.
            # Solo estamos esperando DNS/SSL.
            created[
                "success"
            ] = True

            return created

        # -----------------------------------------------------
        # FALLÓ HEALTH / SSL
        # -----------------------------------------------------

        if not finalized.get(
            "success"
        ):
            created[
                "success"
            ] = False

            created[
                "message"
            ] = (
                finalized.get(
                    "message"
                )
                or
                "Falló la verificación final."
            )

            created[
                "failed_checks"
            ] = (
                finalized.get(
                    "failed_checks"
                )
                or []
            )

            created[
                "checks"
            ] = (
                finalized.get(
                    "checks"
                )
                or {}
            )

            return created

        # -----------------------------------------------------
        # TODO CORRECTO
        # -----------------------------------------------------

        created[
            "message"
        ] = (
            finalized.get(
                "message"
            )
            or
            "Instancia creada correctamente."
        )

        return created

    # ---------------------------------------------------------
    # CONTINUAR DNS / SSL
    # ---------------------------------------------------------

    def provision_finalize(
        self,
        payload,
    ):
        self._allowed_unit(
            payload
        )

        return (
            self.provisioner
            .finalize(
                payload
            )
        )