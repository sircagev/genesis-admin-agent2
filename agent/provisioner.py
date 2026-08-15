import configparser
import os
import re
import secrets
import shlex
import shutil
import socket
import time
import urllib.error
import urllib.request

from pathlib import Path

from .commands import (
    CommandError,
    assert_owner,
    port_available,
    run,
    systemd_exists,
    systemd_status,
)


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"([a-zA-Z0-9]"
    r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)

ODOO_SYSTEM_USER = "odoo"
ODOO_SYSTEM_GROUP = "odoo"


class OdooProvisioner:

    def __init__(self, config):
        self.config = config
        self.pcfg = config.section("provision")

    # ---------------------------------------------------------
    # NORMALIZACIÓN
    # ---------------------------------------------------------

    @staticmethod
    def normalize_unit(unit):
        unit = str(unit or "").strip()

        if not unit:
            raise CommandError(
                "La unidad systemd está vacía."
            )

        if unit.endswith(".service"):
            return unit

        return f"{unit}.service"

    def _run_certbot_with_retry(self, domain, email):
        delays = [0, 5, 10, 15]
        last_error = None

        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)

            try:
                run(
                    [
                        "certbot",
                        "--nginx",
                        "-d",
                        domain,
                        "--non-interactive",
                        "--agree-tos",
                        "--redirect",
                        "-m",
                        email,
                    ],
                    timeout=600,
                )

                return {
                    "success": True,
                    "attempts": attempt,
                }

            except CommandError as exc:
                last_error = exc
                message = str(exc).lower()

                transient = (
                    "another instance of certbot is already running" in message
                    or "certbot.lock" in message
                    or "lock file" in message
                )

                if not transient:
                    raise

                if attempt >= len(delays):
                    raise

        raise last_error

    def owner_from_unit(self, unit):
        prefix = "odoo-server-"

        unit = self.normalize_unit(unit)

        base = (
            unit[:-8]
            if unit.endswith(".service")
            else unit
        )

        if not base.startswith(prefix):
            raise CommandError(
                "La unidad debe iniciar con odoo-server-"
            )

        return assert_owner(
            base[len(prefix):]
        )

    def validate_payload(self, payload):

        unit = self.normalize_unit(
            payload["service_name"]
        )

        owner = self.owner_from_unit(unit)

        version = str(
            payload.get("version_odoo") or "19"
        )

        if version not in {
            "17",
            "18",
            "19",
        }:
            raise CommandError(
                "Versión Odoo permitida: 17, 18 o 19."
            )

        http_port = int(
            payload["http_port"]
        )

        gevent_port = int(
            payload["gevent_port"]
        )

        if http_port == gevent_port:
            raise CommandError(
                "http_port y gevent_port deben ser distintos."
            )

        domain = (
            payload.get("domain") or ""
        ).strip().lower()

        if not DOMAIN_RE.fullmatch(domain):
            raise CommandError(
                f"Dominio inválido: {domain!r}"
            )

        database_name = str(
            payload.get("database_name")
            or ""
        ).strip().lower()

        if (
            database_name
            and not re.fullmatch(
                r"[a-z][a-z0-9_]{0,62}",
                database_name,
            )
        ):
            raise CommandError(
                "database_name debe iniciar con una letra "
                "y usar solamente minúsculas, números "
                "y guiones bajos."
            )

        return {
            **payload,

            "service_name": unit,

            "owner": owner,

            "version_odoo": version,

            "http_port": http_port,

            "gevent_port": gevent_port,

            "workers": int(
                payload.get("workers")
                or 0
            ),

            "max_cron_threads": int(
                payload.get(
                    "max_cron_threads"
                )
                or 0
            ),

            "domain": domain,

            "database_name": database_name,
        }

    # ---------------------------------------------------------
    # VALIDACIÓN INSTANCIA NUEVA
    # ---------------------------------------------------------

    def prepare(self, raw_payload):

        payload = self.validate_payload(
            raw_payload
        )

        checks = {

            "service_not_exists":
                not systemd_exists(
                    payload["service_name"]
                ),

            "http_port_available":
                port_available(
                    payload["http_port"]
                ),

            "gevent_port_available":
                port_available(
                    payload["gevent_port"]
                ),

            "nginx_installed":
                shutil.which(
                    "nginx"
                ) is not None,

            "postgresql_ok":
                run(
                    [
                        "runuser",
                        "-u",
                        "postgres",
                        "--",
                        "psql",
                        "-Atc",
                        "SELECT 1;",
                    ],
                    check=False,
                )["success"],

            "odoo_system_user_exists":
                self._linux_user_exists(
                    ODOO_SYSTEM_USER
                ),

            "odoo_system_group_exists":
                run(
                    [
                        "getent",
                        "group",
                        ODOO_SYSTEM_GROUP,
                    ],
                    check=False,
                )["success"],
        }

        if payload.get(
            "create_ssl"
        ):
            checks[
                "certbot_installed"
            ] = (
                shutil.which(
                    "certbot"
                )
                is not None
            )

        else:
            checks[
                "certbot_installed"
            ] = True

        failed = [
            key
            for key, value
            in checks.items()
            if not value
        ]

        return {
            "success": not failed,

            "checks": checks,

            "failed_checks": failed,

            "normalized": payload,
        }

    # ---------------------------------------------------------
    # ADOPTAR INSTANCIA EXISTENTE
    # ---------------------------------------------------------

    def adopt(self, raw_payload):

        payload = self.validate_payload(
            raw_payload
        )

        unit = payload[
            "service_name"
        ]

        status = systemd_status(
            unit
        )

        service_exists = (
            systemd_exists(unit)
            and status.get(
                "exists",
                False,
            )
        )

        config_path = None

        detected = {}

        config_error = ""

        if service_exists:

            try:

                config_path = (
                    self._detect_odoo_config(
                        unit,
                        status,
                    )
                )

                if config_path:

                    detected = (
                        self._read_odoo_config(
                            config_path
                        )
                    )

                else:

                    config_error = (
                        "No fue posible detectar "
                        "el archivo de configuración "
                        "Odoo usado por la unidad systemd."
                    )

            except Exception as exc:

                config_error = str(
                    exc
                )

        http_detected = (
            self._as_int(
                detected.get(
                    "http_port"
                )
            )
        )

        gevent_detected = (
            self._as_int(
                detected.get(
                    "gevent_port"
                )
                or detected.get(
                    "longpolling_port"
                )
            )
        )

        checks = {

            "service_exists":
                service_exists,

            "config_found":
                bool(
                    config_path
                ),

            "config_readable":
                bool(
                    detected
                ),

            "http_port_matches": (
                http_detected
                == payload[
                    "http_port"
                ]
                if (
                    http_detected
                    is not None
                )
                else False
            ),

            "gevent_port_matches": (
                gevent_detected
                == payload[
                    "gevent_port"
                ]
                if (
                    gevent_detected
                    is not None
                )
                else False
            ),
        }

        info = {

            "service_active":
                status.get(
                    "active_state"
                )
                == "active",

            "http_port_listening":
                not port_available(
                    payload[
                        "http_port"
                    ]
                ),

            "gevent_port_listening":
                not port_available(
                    payload[
                        "gevent_port"
                    ]
                ),
        }

        failed = [
            key
            for key, value
            in checks.items()
            if not value
        ]

        success = not failed

        result = {

            "success": success,

            "message": (
                "Servicio existente "
                "adoptado correctamente."
                if success
                else
                "No fue posible adoptar "
                "el servicio existente."
            ),

            "checks": checks,

            "info": info,

            "failed_checks":
                failed,

            "status":
                status,

            "unit":
                unit,

            "owner":
                payload["owner"],

            "config_path": (
                str(config_path)
                if config_path
                else ""
            ),

            "detected":
                detected,

            "expected": {

                "http_port":
                    payload[
                        "http_port"
                    ],

                "gevent_port":
                    payload[
                        "gevent_port"
                    ],
            },
        }

        if config_error:

            result[
                "config_error"
            ] = config_error

        return result

    # ---------------------------------------------------------
    # UTILIDADES ADOPCIÓN
    # ---------------------------------------------------------

    @staticmethod
    def _as_int(value):

        if (
            value is None
            or value == ""
        ):
            return None

        try:
            return int(
                str(value).strip()
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

    def _detect_odoo_config(
        self,
        unit,
        status,
    ):

        main_pid = int(
            status.get(
                "main_pid"
            )
            or 0
        )

        # -----------------------------------------------------
        # 1. PROCESO ACTUAL
        # -----------------------------------------------------

        if main_pid > 0:

            cmdline = Path(
                f"/proc/{main_pid}/cmdline"
            )

            if cmdline.exists():

                args = [

                    part.decode(
                        "utf-8",
                        errors="replace",
                    )

                    for part
                    in cmdline.read_bytes().split(
                        b"\0"
                    )

                    if part
                ]

                found = (
                    self._config_from_argv(
                        args
                    )
                )

                if found:

                    path = Path(
                        found
                    )

                    if path.is_file():
                        return path

        # -----------------------------------------------------
        # 2. SYSTEMCTL SHOW
        # -----------------------------------------------------

        result = run(
            [
                "systemctl",
                "show",
                unit,
                "--property=ExecStart",
                "--value",
            ],
            check=False,
        )

        text = (
            result.get(
                "output"
            )
            or ""
        )

        found = (
            self._config_from_execstart_text(
                text
            )
        )

        if found:

            path = Path(
                found
            )

            if path.is_file():
                return path

        # -----------------------------------------------------
        # 3. ARCHIVO UNIT
        # -----------------------------------------------------

        fragment = run(
            [
                "systemctl",
                "show",
                unit,
                "--property=FragmentPath",
                "--value",
            ],
            check=False,
        ).get(
            "output",
            "",
        ).strip()

        if (
            fragment
            and Path(
                fragment
            ).is_file()
        ):

            for raw_line in (
                Path(fragment)
                .read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                .splitlines()
            ):

                line = (
                    raw_line.strip()
                )

                if line.startswith(
                    "ExecStart="
                ):

                    found = (
                        self._config_from_execstart_text(
                            line[
                                len(
                                    "ExecStart="
                                ):
                            ]
                        )
                    )

                    if found:

                        path = Path(
                            found
                        )

                        if path.is_file():
                            return path

        return None

    @staticmethod
    def _config_from_argv(
        args
    ):

        for index, value in enumerate(
            args
        ):

            if (
                value
                in (
                    "-c",
                    "--config",
                )
                and index + 1
                < len(args)
            ):

                return args[
                    index + 1
                ]

            if value.startswith(
                "--config="
            ):

                return value.split(
                    "=",
                    1,
                )[1]

        return None

    @staticmethod
    def _config_from_execstart_text(
        text
    ):

        try:

            candidates = (
                shlex.split(
                    text
                )
            )

        except ValueError:

            candidates = (
                text.split()
            )

        found = (
            OdooProvisioner
            ._config_from_argv(
                candidates
            )
        )

        if found:

            return (
                found.strip(
                    "'\";"
                )
            )

        patterns = [

            r"(?:^|\s)-c\s+([^\s;]+)",

            r"(?:^|\s)--config\s+([^\s;]+)",

            r"(?:^|\s)--config=([^\s;]+)",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
            )

            if match:

                return (
                    match.group(
                        1
                    ).strip(
                        "'\";"
                    )
                )

        return None

    @staticmethod
    def _read_odoo_config(path):

        parser = (
            configparser
            .RawConfigParser(
                interpolation=None,
                strict=False,
            )
        )

        try:

            with Path(path).open(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                parser.read_file(handle)

        except (
            OSError,
            configparser.Error,
        ) as exc:

            raise CommandError(
                "No se pudo leer "
                f"la configuración Odoo "
                f"{path}: {exc}"
            ) from exc

        if not parser.has_section(
            "options"
        ):

            raise CommandError(
                f"El archivo {path} "
                "no contiene la sección "
                "[options]."
            )

        keys = (
            "http_port",
            "xmlrpc_port",
            "gevent_port",
            "longpolling_port",
            "workers",
            "max_cron_threads",
            "http_interface",
            "xmlrpc_interface",
            "proxy_mode",
            "logfile",
            "log_level",
            "addons_path",
            "db_user",
            "db_name",
            "dbfilter",
            "data_dir",
        )

        return {

            key:
                parser.get(
                    "options",
                    key,
                )

            for key
            in keys

            if parser.has_option(
                "options",
                key,
            )
        }

    # ---------------------------------------------------------
    # CREAR INSTANCIA NUEVA
    # ---------------------------------------------------------

    def create(
        self,
        raw_payload,
        progress_callback=None,
    ):

        def progress(
            stage,
            percent,
            message,
        ):
            if callable(progress_callback):
                progress_callback(
                    stage,
                    percent,
                    message,
                )

        prepared = self.prepare(raw_payload)

        if not prepared["success"]:

            return {
                "success": False,
                "message": "Fallaron validaciones previas.",
                **prepared,
            }

        payload = prepared["normalized"]

        if payload.get("dry_run", True):
            return {
                "success":  True,
                "dry_run":  True,
                "message":
                    "Dry-run correcto; "
                    "no se realizaron cambios.",
                "plan": self._plan(payload),
            }

        owner = payload["owner"]

        base_dir = (
            Path(
                self.pcfg.get("base_dir")
                or "/opt"
            )
            / owner
        )

        odoo_dir = (
            base_dir
            / "odoo-server"
        )

        venv = (
            odoo_dir
            / "venv"
        )

        custom_dir = (
            odoo_dir
            / (
                "modulosFE"
                f"{payload['version_odoo']}"
            )
        )

        conf_path = Path(
            f"/etc/odoo{owner}.conf"
        )

        log_path = Path(

            payload.get("logfile")

            or

            f"/var/log/odoo/"
            f"odoo-server-{owner}.log"
        )

        systemd_path = Path(
            "/etc/systemd/system/"
            f"{payload['service_name']}"
        )

        nginx_path = Path(
            "/etc/nginx/"
            "sites-available/"
            f"{owner}.conf"
        )

        nginx_link = Path(
            "/etc/nginx/"
            "sites-enabled/"
            f"{owner}.conf"
        )

        steps = []

        base_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # -----------------------------------------------------
        # ODOO
        # -----------------------------------------------------

        progress(
            "clone",
            40,
            "Preparando código fuente de Odoo...",
        )

        if not (
            odoo_dir
            / ".git"
        ).exists():

            run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    (
                        f"{payload['version_odoo']}"
                        ".0"
                    ),
                    (
                        self.pcfg.get(
                            "odoo_repo"
                        )
                        or
                        "https://github.com/"
                        "odoo/odoo.git"
                    ),
                    str(
                        odoo_dir
                    ),
                ],
                timeout=1800,
            )

            steps.append(
                "Odoo clonado"
            )

        progress(
            "clone",
            46,
            "Código fuente de Odoo disponible.",
        )

        # -----------------------------------------------------
        # VENV
        # -----------------------------------------------------

        progress(
            "dependencies",
            50,
            "Preparando entorno virtual y dependencias...",
        )

        if not venv.exists():

            run(
                [
                    "python3",
                    "-m",
                    "venv",
                    str(venv),
                ],
                timeout=120,
            )

            run(
                [
                    str(
                        venv
                        / "bin/pip"
                    ),
                    "install",
                    "--upgrade",
                    "pip",
                    "wheel",
                ],
                timeout=600,
            )

            run(
                [
                    str(
                        venv
                        / "bin/pip"
                    ),
                    "install",
                    "-r",
                    str(
                        odoo_dir
                        / "requirements.txt"
                    ),
                ],
                timeout=1800,
            )

            steps.append(
                "Virtualenv y "
                "requirements instalados"
            )

            progress(
                "dependencies",
                58,
                "Dependencias de Odoo instaladas.",
            )

        # -----------------------------------------------------
        # CUSTOM MODULES
        # -----------------------------------------------------

        custom_repo = (
            self.pcfg.get(
                "custom_addons_repo"
            )
            or ""
        ).strip()

        if (
            custom_repo
            and not (
                custom_dir
                / ".git"
            ).exists()
        ):

            self._clone_custom_repo(
                custom_repo,
                custom_dir,
            )

            steps.append(
                "Repositorio de módulos clonado"
            )

        # -----------------------------------------------------
        # LOG
        # -----------------------------------------------------

        log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        log_path.touch(
            exist_ok=True
        )

        # -----------------------------------------------------
        # POSTGRES
        # -----------------------------------------------------

        progress(
            "postgres",
            62,
            "Preparando rol PostgreSQL...",
        )

        self._ensure_postgres_role(
            owner
        )

        steps.append(
            "Rol PostgreSQL verificado"
        )

        # -----------------------------------------------------
        # ADDONS
        # -----------------------------------------------------

        addons = [

            str(
                odoo_dir
                / "odoo/addons"
            ),

            str(
                odoo_dir
                / "addons"
            ),
        ]

        if custom_dir.exists():

            subpaths = (
                self.pcfg.get(
                    "custom_addons_subpaths"
                )
                or [
                    "custom_addons",
                    "modulos",
                ]
            )

            found_custom = False

            for subpath in subpaths:

                candidate = (
                    custom_dir
                    / str(subpath)
                )

                if candidate.exists():

                    addons.append(
                        str(candidate)
                    )

                    found_custom = True

            if not found_custom:

                addons.append(
                    str(custom_dir)
                )

        # -----------------------------------------------------
        # ODOO.CONF
        # -----------------------------------------------------

        progress(
            "config",
            68,
            "Generando configuración de Odoo...",
        )

        conf_path.write_text(
            self._odoo_conf(
                payload,
                owner,
                addons,
                log_path,
            ),
            encoding="utf-8",
        )

        os.chmod(
            conf_path,
            0o640,
        )

        # -----------------------------------------------------
        # SYSTEMD
        # -----------------------------------------------------

        systemd_path.write_text(
            self._systemd_unit(
                payload,
                owner,
                odoo_dir,
                venv,
                conf_path,
            ),
            encoding="utf-8",
        )

        steps.append(
            "Configuración Odoo "
            "y systemd creadas"
        )

        # -----------------------------------------------------
        # NGINX
        # -----------------------------------------------------

        progress(
            "nginx",
            75,
            "Configurando Nginx...",
        )

        if payload.get(
            "create_nginx",
            True,
        ):

            nginx_path.write_text(
                self._nginx_conf(
                    payload,
                    owner,
                ),
                encoding="utf-8",
            )

            if not nginx_link.exists():

                nginx_link.symlink_to(
                    nginx_path
                )

            run(
                [
                    "nginx",
                    "-t",
                ]
            )

            run(
                [
                    "systemctl",
                    "reload",
                    "nginx",
                ]
            )

            steps.append(
                "Nginx configurado"
            )

        # -----------------------------------------------------
        # OWNERSHIP
        # -----------------------------------------------------

        progress(
            "service",
            78,
            "Aplicando permisos del servicio...",
        )

        run(
            [
                "chown",
                "-R",
                f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}",
                str(base_dir),
            ]
        )

        run(
            [
                "chown",
                f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}",
                str(log_path),
            ]
        )

        run(
            [
                "chown",
                f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}",
                str(conf_path),
            ]
        )

        os.chmod(
            conf_path,
            0o640,
        )

        steps.append(
            "Permisos asignados a odoo:odoo"
        )

        # -----------------------------------------------------
        # SYSTEMD
        # -----------------------------------------------------

        run(
            [
                "systemctl",
                "daemon-reload",
            ]
        )

        run(
            [
                "systemctl",
                "enable",
                payload[
                    "service_name"
                ],
            ]
        )

        if payload.get(
            "start_service",
            True,
        ):

            run(
                [
                    "systemctl",
                    "restart",
                    payload[
                        "service_name"
                    ],
                ],
                timeout=120,
            )

            steps.append(
                "Servicio Odoo iniciado"
            )

        progress(
            "service",
            82,
            "Servicio Odoo iniciado.",
        )

        # -----------------------------------------------------
        # SSL
        # -----------------------------------------------------

        if payload.get(
            "create_ssl",
            True,
        ):

            email = (

                payload.get(
                    "certbot_email"
                )

                or

                self.pcfg.get(
                    "certbot_email"
                )

                or ""

            ).strip()

            if not email:

                raise CommandError(
                    "create_ssl está activo "
                    "pero no hay certbot_email "
                    "configurado."
                )

            run(
                [
                    "certbot",
                    "--nginx",
                    "-d",
                    payload[
                        "domain"
                    ],
                    "--non-interactive",
                    "--agree-tos",
                    "--redirect",
                    "-m",
                    email,
                ],
                timeout=600,
            )

            steps.append(
                "Certificado SSL instalado"
            )

        return {
            "success": True,
            "dry_run": False,
            "message": "Instancia Odoo base creada.",
            "owner": owner,
            "unit": payload["service_name"],
            "base_dir": str(base_dir),
            "config": str(conf_path),
            "logfile": str(log_path),
            "http_port": payload["http_port"],
            "gevent_port": payload["gevent_port"],
            "domain": payload["domain"],
            "steps": steps,
        }

    def finalize(self, raw_payload, progress_callback=None):
        """
        Finaliza una instancia ya creada:

        1. Comprueba DNS público.
        2. Espera si todavía no propagó.
        3. Configura SSL.
        4. Valida Nginx.
        5. Hace health check.
        """
        def progress(
            stage,
            percent,
            message,
        ):
            if callable(progress_callback):
                progress_callback(
                    stage,
                    percent,
                    message,
                )

        payload = self.validate_payload(
            raw_payload
        )

        unit = payload[
            "service_name"
        ]

        # ---------------------------------------------------------
        # VERIFICAR SYSTEMD
        # ---------------------------------------------------------

        if not systemd_exists(unit):
            return {
                "success": False,

                "message":
                    "La unidad systemd "
                    "todavía no existe.",

                "waiting_dns":
                    False,

                "status":
                    systemd_status(
                        unit
                    ),
            }

        # ---------------------------------------------------------
        # IP ESPERADA
        # ---------------------------------------------------------

        expected_ip = str(
            payload.get(
                "expected_ipv4"
            )
            or ""
        ).strip()

        # ---------------------------------------------------------
        # DNS PÚBLICO
        # ---------------------------------------------------------

        progress(
            "waiting_dns",
            85,
            "Comprobando propagación DNS...",
        )

        resolved = (
            self._resolve_ipv4(
                payload[
                    "domain"
                ]
            )
        )

        # ---------------------------------------------------------
        # TODAVÍA NO PROPAGÓ
        # ---------------------------------------------------------

        if (
            expected_ip
            and expected_ip
            not in resolved
        ):
            return {
                "success":
                    True,

                "waiting_dns":
                    True,

                "message": (
                    "Instancia creada. "
                    "Esperando propagación DNS: "
                    f"{payload['domain']} "
                    "debe resolver a "
                    f"{expected_ip}."
                ),

                "dns_addresses":
                    resolved,

                "expected_ipv4":
                    expected_ip,

                "status":
                    systemd_status(
                        unit
                    ),

                "health":
                    self._health(
                        payload
                    ),
            }

        steps = []

        # ---------------------------------------------------------
        # SSL
        # ---------------------------------------------------------

        if payload.get(
            "create_ssl",
            True,
        ):
            email = (
                payload.get(
                    "certbot_email"
                )

                or self.pcfg.get(
                    "certbot_email"
                )

                or ""
            ).strip()

            if not email:
                return {
                    "success":
                        False,

                    "waiting_dns":
                        False,

                    "message": (
                        "El DNS ya está listo, "
                        "pero falta certbot_email "
                        "para emitir el certificado."
                    ),

                    "dns_addresses":
                        resolved,

                    "status":
                        systemd_status(
                            unit
                        ),
                }

            if (
                shutil.which(
                    "certbot"
                )
                is None
            ):
                return {
                    "success":
                        False,

                    "waiting_dns":
                        False,

                    "message":
                        "Certbot no está instalado.",

                    "dns_addresses":
                        resolved,
                }

            # -----------------------------------------------------
            # CERTBOT
            # -----------------------------------------------------

            progress(
                "ssl",
                90,
                "Configurando certificado SSL...",
            )

            certbot_result = self._run_certbot_with_retry(
                payload["domain"],
                email,
            )

            steps.append(
                f"SSL configurado con Certbot "
                f"(intentos: {certbot_result['attempts']})"
            )

            progress(
                "ssl",
                94,
                "Certificado SSL configurado.",
            )

            # -----------------------------------------------------
            # VALIDAR NGINX
            # -----------------------------------------------------

            run(
                [
                    "nginx",
                    "-t",
                ],
                timeout=30,
            )

            run(
                [
                    "systemctl",
                    "reload",
                    "nginx",
                ],
                timeout=60,
            )


        # ---------------------------------------------------------
        # HEALTH CHECK
        # ---------------------------------------------------------

        progress(
            "health",
            97,
            "Verificando servicio, puertos y HTTP...",
        )

        health = self._health(
            payload
        )

        checks = {
            "service_active":
                health[
                    "service_active"
                ],

            "http_listening":
                health[
                    "http_listening"
                ],

            "gevent_listening":
                health[
                    "gevent_listening"
                ],

            "odoo_http_responding":
                health[
                    "odoo_http_responding"
                ],
        }

        failed = [
            key

            for key, value
            in checks.items()

            if not value
        ]

        if not failed:
            progress(
                "finished",
                100,
                "Instancia creada y verificada correctamente.",
            )

        return {
            "success":
                not failed,

            "waiting_dns":
                False,

            "message": (
                "Instancia Odoo creada "
                "y verificada correctamente."

                if not failed

                else

                "La instancia fue creada, "
                "pero falló la verificación."
            ),

            "dns_addresses":
                resolved,

            "expected_ipv4":
                expected_ip,

            "checks":
                checks,

            "failed_checks":
                failed,

            "health":
                health,

            "status":
                health[
                    "status"
                ],

            "steps":
                steps,
        }


    @staticmethod
    def _resolve_ipv4(
        domain,
    ):
        values = set()

        try:
            for item in (
                socket.getaddrinfo(
                    domain,
                    None,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
            ):
                if (
                    item
                    and item[4]
                ):
                    values.add(
                        item[4][0]
                    )

        except socket.gaierror:
            pass

        return sorted(
            values
        )

    def _health(
        self,
        payload,
    ):
        unit = payload[
            "service_name"
        ]

        status = systemd_status(
            unit
        )

        # ---------------------------------------------------------
        # PUERTOS
        # ---------------------------------------------------------

        http_listening = (
            not port_available(
                payload[
                    "http_port"
                ]
            )
        )

        gevent_listening = (
            not port_available(
                payload[
                    "gevent_port"
                ]
            )
        )

        # ---------------------------------------------------------
        # HTTP ODOO
        # ---------------------------------------------------------

        responding = False
        http_status = 0
        last_error = ""

        url = (
            "http://127.0.0.1:"
            f"{payload['http_port']}"
            "/web/login"
        )

        # ---------------------------------------------------------
        # REINTENTAR CINCO VECES
        # ---------------------------------------------------------

        for _attempt in range(5):

            try:
                request = (
                    urllib.request.Request(
                        url,
                        headers={
                            "Host":
                                payload[
                                    "domain"
                                ]
                        },
                    )
                )

                with (
                    urllib.request
                    .urlopen(
                        request,
                        timeout=5,
                    )
                ) as response:

                    http_status = int(
                        response.getcode()
                        or 0
                    )

                    responding = (
                        200
                        <= http_status
                        < 500
                    )

                    if responding:
                        break

            except urllib.error.HTTPError as exc:

                http_status = int(
                    exc.code
                    or 0
                )

                responding = (
                    200
                    <= http_status
                    < 500
                )

                last_error = str(
                    exc
                )

                if responding:
                    break

            except Exception as exc:
                # pylint: disable=broad-except

                last_error = str(
                    exc
                )

            time.sleep(
                2
            )

        return {
            "status":
                status,

            "service_active": (
                status.get(
                    "active_state"
                )
                == "active"
            ),

            "http_listening":
                http_listening,

            "gevent_listening":
                gevent_listening,

            "odoo_http_responding":
                responding,

            "http_status":
                http_status,

            "http_error":
                last_error,
        }
    # ---------------------------------------------------------
    # PLAN
    # ---------------------------------------------------------

    def _plan(
        self,
        payload,
    ):

        owner = (
            payload[
                "owner"
            ]
        )

        return {

            "owner":
                owner,

            "unit":
                payload[
                    "service_name"
                ],

            "odoo_branch":
                (
                    f"{payload['version_odoo']}"
                    ".0"
                ),

            "base_dir":
                (
                    f"{self.pcfg.get('base_dir') or '/opt'}"
                    f"/{owner}"
                ),

            "http_port":
                payload[
                    "http_port"
                ],

            "gevent_port":
                payload[
                    "gevent_port"
                ],

            "workers":
                payload[
                    "workers"
                ],

            "max_cron_threads":
                payload[
                    "max_cron_threads"
                ],

            "domain":
                payload[
                    "domain"
                ],

            "create_nginx":
                payload.get(
                    "create_nginx",
                    True,
                ),

            "create_ssl":
                payload.get(
                    "create_ssl",
                    True,
                ),
        }

    # ---------------------------------------------------------
    # UTILIDADES
    # ---------------------------------------------------------

    @staticmethod
    def _linux_user_exists(
        owner
    ):

        return run(
            [
                "id",
                owner,
            ],
            check=False,
        )["success"]

    @staticmethod
    def _ensure_postgres_role(
        owner
    ):

        sql = (

            "DO $$ BEGIN "

            "IF NOT EXISTS "
            "(SELECT 1 "
            "FROM pg_roles "
            f"WHERE rolname = '{owner}') "

            "THEN "

            f'CREATE ROLE "{owner}" '
            "LOGIN CREATEDB; "

            "END IF; "

            "END $$;"
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ]
        )

    def _clone_custom_repo(
        self,
        repo,
        target,
    ):

        env = os.environ.copy()

        token = (
            env.get(
                "GITHUB_TOKEN"
            )
            or ""
        ).strip()

        branch = (
            self.pcfg.get(
                "custom_addons_branch"
            )
            or ""
        ).strip()

        cmd = [
            "git",
            "clone",
        ]

        if branch:

            cmd += [
                "--branch",
                branch,
            ]

        if (
            token
            and repo.startswith(
                "https://github.com/"
            )
        ):

            askpass = Path(
                "/tmp/"
                "genesis-agent-git-askpass.sh"
            )

            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Username*) '
                'echo "x-access-token" ;;\n'
                '  *Password*) '
                'echo "$GITHUB_TOKEN" ;;\n'
                "esac\n",
                encoding="utf-8",
            )

            os.chmod(
                askpass,
                0o700,
            )

            env[
                "GIT_ASKPASS"
            ] = str(
                askpass
            )

            env[
                "GIT_TERMINAL_PROMPT"
            ] = "0"

            try:

                run(
                    cmd
                    + [
                        repo,
                        str(target),
                    ],
                    timeout=1200,
                    env=env,
                )

            finally:

                askpass.unlink(
                    missing_ok=True
                )

        else:

            run(
                cmd
                + [
                    repo,
                    str(target),
                ],
                timeout=1200,
                env=env,
            )

    # ---------------------------------------------------------
    # GENERAR ODOO.CONF
    # ---------------------------------------------------------

    @staticmethod
    def _odoo_conf(
        payload,
        owner,
        addons,
        log_path,
    ):
        version = str(
            payload.get(
                "version_odoo"
            )
            or "19"
        )

        interface = (
            payload.get(
                "http_interface"
            )
            or "127.0.0.1"
        )

        # ---------------------------------------------------------
        # ODOO 17
        # ---------------------------------------------------------

        if version == "17":
            port_lines = [
                (
                    "xmlrpc_port = "
                    f"{payload['http_port']}"
                ),

                (
                    "longpolling_port = "
                    f"{payload['gevent_port']}"
                ),

                (
                    "xmlrpc_interface = "
                    f"{interface}"
                ),
            ]

        # ---------------------------------------------------------
        # ODOO 18 / 19
        # ---------------------------------------------------------

        else:
            port_lines = [
                (
                    "http_port = "
                    f"{payload['http_port']}"
                ),

                (
                    "gevent_port = "
                    f"{payload['gevent_port']}"
                ),

                (
                    "http_interface = "
                    f"{interface}"
                ),
            ]

        # ---------------------------------------------------------
        # CONFIG
        # ---------------------------------------------------------

        database_name = str(
            payload.get("database_name")
            or ""
        ).strip()

        database_lines = []

        if database_name:
            database_lines.append(
                f"dbfilter = ^{database_name}$"
            )

        return "\n".join(
            [
                "[options]",

                (
                    "admin_passwd = "
                    f"{secrets.token_urlsafe(32)}"
                ),

                (
                    "db_user = "
                    f"{owner}"
                ),

                "db_password = False",

                *database_lines,

                (
                    "addons_path = "
                    f"{','.join(addons)}"
                ),

                *port_lines,

                (
                    "workers = "
                    f"{payload['workers']}"
                ),

                (
                    "max_cron_threads = "
                    f"{payload['max_cron_threads']}"
                ),

                (
                    "proxy_mode = "
                    f"{bool(payload.get('proxy_mode', True))}"
                ),

                (
                    "logfile = "
                    f"{log_path}"
                ),

                (
                    "log_level = "
                    f"{payload.get('log_level') or 'info'}"
                ),

                "list_db = True",

                "",
            ]
        )
    # ---------------------------------------------------------
    # SYSTEMD
    # ---------------------------------------------------------

    @staticmethod
    def _systemd_unit(
        payload,
        owner,
        odoo_dir,
        venv,
        conf_path,
    ):

        return f"""[Unit]
            Description=Odoo {owner}
            After=network.target postgresql.service
            Wants=postgresql.service

            [Service]
            Type=simple
            User={ODOO_SYSTEM_USER}
            Group={ODOO_SYSTEM_GROUP}
            WorkingDirectory={odoo_dir}
            ExecStart={venv}/bin/python {odoo_dir}/odoo-bin -c {conf_path}
            Restart=always
            RestartSec=5
            LimitNOFILE=65536

            [Install]
            WantedBy=multi-user.target
        """

    # ---------------------------------------------------------
    # NGINX
    # ---------------------------------------------------------

    @staticmethod
    def _nginx_conf(
        payload,
        owner,
    ):

        upstream = (
            "odoo_"
            + owner.replace(
                "-",
                "_",
            )
        )

        chat = (
            upstream
            + "_chat"
        )

        domain = (
            payload[
                "domain"
            ]
        )

        return f"""upstream {upstream} {{
                server 127.0.0.1:{payload['http_port']};
            }}

            upstream {chat} {{
                server 127.0.0.1:{payload['gevent_port']};
            }}

            server {{
                listen 80;
                server_name {domain};

                proxy_read_timeout 720s;
                proxy_connect_timeout 720s;
                proxy_send_timeout 720s;

                proxy_set_header X-Forwarded-Host $host;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Forwarded-Proto $scheme;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header Host $host;

                location /websocket {{
                    proxy_pass http://{chat};

                    proxy_set_header Upgrade $http_upgrade;

                    proxy_set_header Connection "upgrade";
                }}

                location / {{
                    proxy_pass http://{upstream};
                    proxy_redirect off;
                }}
            }}
            """