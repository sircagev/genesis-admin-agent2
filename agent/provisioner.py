import configparser
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import time
import tempfile
import urllib.error
import urllib.request

from pathlib import Path
from urllib.parse import urlsplit

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
GITHUB_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,255}$")


class OdooProvisioner:

    def __init__(self, config):
        self.config = config
        self.base_pcfg = dict(config.section("provision"))
        self.pcfg = dict(self.base_pcfg)

    def set_runtime_config(self, values):
        self.pcfg = dict(self.base_pcfg)
        allowed = {
            "postgres_host",
            "postgres_port",
            "postgres_default_password",
            "postgres_validate_auth",
            "odoo_repo",
            "odoo_branch",
            "admin_passwd",
            "custom_addons_repo",
            "custom_addons_branch",
            "github_auth_enabled",
            "github_username",
            "github_token",
        }
        for key, value in (values or {}).items():
            if key in allowed:
                self.pcfg[key] = value

    def clear_runtime_config(self):
        self.pcfg = dict(self.base_pcfg)

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
            "admin_passwd",
            "db_host",
            "db_port",
            "db_user",
            "db_password",
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
        def progress(stage, percent, message):
            if callable(progress_callback):
                progress_callback(stage, percent, message)

        progress("ports", 18, "Validando puertos y prerrequisitos...")
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
                "success": True,
                "dry_run": True,
                "message": "Dry-run correcto; no se realizaron cambios.",
                "plan": self._plan(payload),
            }

        owner = payload["owner"]
        base_root = Path(self.pcfg.get("base_dir") or "/opt").resolve()
        base_dir = (base_root / owner).resolve()
        if base_dir.parent != base_root:
            raise CommandError("Ruta de instancia no permitida.")

        odoo_dir = base_dir / "odoo-server"
        venv = odoo_dir / f"{owner}venv"
        custom_dir = odoo_dir / f"modulosFE{payload['version_odoo']}"
        conf_path = Path(f"/etc/odoo{owner}.conf")
        log_path = Path(
            payload.get("logfile")
            or f"/var/log/odoo/odoo-server-{owner}.log"
        )
        systemd_path = Path(
            f"/etc/systemd/system/{payload['service_name']}"
        )
        nginx_path = Path(f"/etc/nginx/sites-available/{owner}.conf")
        nginx_link = Path(f"/etc/nginx/sites-enabled/{owner}.conf")
        steps = []

        base_dir.mkdir(parents=True, exist_ok=True)

        progress("clone", 22, "Clonando o validando Odoo upstream...")
        odoo_created = self._prepare_odoo_tree(
            odoo_dir,
            base_dir,
            str(self.pcfg.get("odoo_repo") or "https://github.com/odoo/odoo.git"),
            str(self.pcfg.get("odoo_branch") or f"{payload['version_odoo']}.0"),
        )
        steps.append("Odoo clonado" if odoo_created else "Odoo existente validado")

        custom_repo = str(self.pcfg.get("custom_addons_repo") or "").strip()
        if custom_repo:
            if self._as_bool(self.pcfg.get("github_auth_enabled"), False):
                progress("github_auth", 26, "Autenticando temporalmente con GitHub...")
            progress("custom_repo", 27, "Preparando repositorio de módulos custom...")
            custom_action = self._prepare_custom_repo(custom_repo, custom_dir)
            progress("custom_addons", 29, "Preparando rutas de addons custom...")
            steps.append(custom_action)

        progress("venv", 31, "Preparando entorno virtual de la instancia...")
        if not venv.exists():
            run(["python3", "-m", "venv", str(venv)], timeout=120)
        pip = str(venv / "bin/pip")
        run([pip, "install", "--upgrade", "pip", "wheel"], timeout=600)

        progress("requirements", 35, "Instalando requirements base de Odoo...")
        run([pip, "install", "-r", str(odoo_dir / "requirements.txt")], timeout=1800)
        custom_requirements = custom_dir / "requirements.txt"
        if custom_requirements.is_file():
            progress("custom_requirements", 37, "Instalando requirements custom...")
            run([pip, "install", "-r", str(custom_requirements)], timeout=1800)
        steps.append("Virtualenv y requirements instalados")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)

        progress("postgres", 42, "Configurando rol PostgreSQL...")
        self._ensure_postgres_role(owner)
        if self._as_bool(self.pcfg.get("postgres_validate_auth"), True):
            progress("postgres_auth", 46, "Validando autenticación TCP PostgreSQL...")
            self._validate_postgres_auth(owner, "postgres")
        steps.append("Rol y autenticación PostgreSQL verificados")

        addons = [
            str(odoo_dir / "addons"),
            str(custom_dir / "custom_addons"),
            str(custom_dir / "modulos"),
        ]

        progress("config", 50, "Generando y validando configuración de Odoo...")
        conf_path.write_text(
            self._odoo_conf(payload, owner, addons, log_path),
            encoding="utf-8",
        )
        os.chmod(conf_path, 0o640)
        self._validate_odoo_conf(conf_path, payload)

        systemd_path.write_text(
            self._systemd_unit(
                payload, owner, odoo_dir, venv, conf_path
            ),
            encoding="utf-8",
        )
        steps.append("Configuración Odoo validada y systemd creado")

        progress("nginx", 55, "Configurando Nginx...")
        if payload.get("create_nginx", True):
            nginx_path.write_text(
                self._nginx_conf(payload, owner),
                encoding="utf-8",
            )
            if not nginx_link.exists():
                nginx_link.symlink_to(nginx_path)
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
            steps.append("Nginx configurado")

        progress("permissions", 58, "Aplicando permisos odoo:odoo...")
        run(["chown", "-R", f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}", str(base_dir)])
        run(["chown", f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}", str(log_path)])
        run(["chown", f"{ODOO_SYSTEM_USER}:{ODOO_SYSTEM_GROUP}", str(conf_path)])
        os.chmod(conf_path, 0o640)

        progress("service", 60, "Registrando servicio Odoo...")
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", payload["service_name"]])
        if payload.get("start_service", True):
            run(["systemctl", "restart", payload["service_name"]], timeout=120)
            steps.append("Servicio Odoo iniciado")

        if payload.get("create_ssl", True):
            email = str(
                payload.get("certbot_email")
                or self.pcfg.get("certbot_email")
                or ""
            ).strip()
            if not email:
                raise CommandError(
                    "create_ssl está activo pero no hay certbot_email configurado."
                )
            progress("ssl", 70, "Configurando certificado SSL...")
            self._run_certbot_with_retry(payload["domain"], email)
            steps.append("Certificado SSL instalado")

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
            65,
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
                70,
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
                72,
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
            75,
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
    def _as_bool(value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _safe_config_value(value, label):
        value = str(value or "")
        if not value or "\n" in value or "\r" in value:
            raise CommandError(f"{label} no puede estar vacío ni contener saltos de línea.")
        return value

    @staticmethod
    def _normalize_repo_url(value):
        value = str(value or "").strip().rstrip("/")
        if value.endswith(".git"):
            value = value[:-4]
        return value.lower()

    def _git_environment(self, repo):
        env = os.environ.copy()
        token = ""
        if (
            self._as_bool(self.pcfg.get("github_auth_enabled"), False)
            and str(repo).startswith("https://github.com/")
        ):
            token = str(self.pcfg.get("github_token") or os.getenv("GITHUB_TOKEN") or "").strip()
        if not token:
            if (
                self._as_bool(self.pcfg.get("github_auth_enabled"), False)
                and str(repo).startswith("https://github.com/")
            ):
                raise CommandError(
                    "La autenticación GitHub está activa, pero el token no está configurado."
                )
            env["GIT_TERMINAL_PROMPT"] = "0"
            return env, None, ""

        if not GITHUB_TOKEN_RE.fullmatch(token):
            raise CommandError(
                "El token GitHub configurado contiene usuario, email, URL "
                "o caracteres no permitidos. Guarde únicamente el PAT."
            )

        username = str(self.pcfg.get("github_username") or "").strip()
        if not username:
            parsed = urlsplit(str(repo))
            path_parts = parsed.path.strip("/").split("/")
            username = (
                path_parts[0]
                if path_parts and path_parts[0]
                else "x-access-token"
            )
        directory = Path(tempfile.mkdtemp(prefix="genesis-git-askpass-"))
        os.chmod(directory, 0o700)
        askpass = directory / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' \"$GITHUB_USERNAME\" ;;\n"
            "  *Password*) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        os.chmod(askpass, 0o700)
        env.update(
            GIT_ASKPASS=str(askpass),
            GIT_TERMINAL_PROMPT="0",
            GITHUB_USERNAME=username,
            GITHUB_TOKEN=token,
            GIT_MERGE_AUTOEDIT="no",
        )
        return env, directory, token

    def _run_git(self, cmd, repo, timeout=1200, check=True):
        env, askpass_dir, token = self._git_environment(repo)
        try:
            return run(cmd, timeout=timeout, check=check, env=env)
        except CommandError as exc:
            message = str(exc)
            if token:
                message = message.replace(token, "***")
            lowered = message.lower()
            if (
                "authentication failed" in lowered
                or "invalid username or password" in lowered
                or "invalid username or token" in lowered
            ):
                friendly = (
                    "GitHub rechazó la autenticación. Verifique usuario, "
                    "token y permisos sobre el repositorio."
                )
            elif "repository not found" in lowered:
                friendly = "Repositorio no encontrado o sin permisos de acceso."
            elif "permission denied" in lowered or "access denied" in lowered:
                friendly = "GitHub denegó el acceso al repositorio."
            elif "remote branch" in lowered and "not found" in lowered:
                friendly = "La rama configurada no existe en el repositorio."
            elif "already exists and is not an empty directory" in lowered:
                friendly = "El destino Git ya existe y no está vacío."
            else:
                friendly = "Falló la descarga o actualización del repositorio."
            raise CommandError(f"{friendly} Detalle: {message}") from exc
        finally:
            env.pop("GITHUB_TOKEN", None)
            if askpass_dir:
                shutil.rmtree(askpass_dir, ignore_errors=True)

    def _validate_git_access(self, repo):
        self._run_git(
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                repo,
                "HEAD",
            ],
            repo,
            timeout=120,
        )

    def _prepare_odoo_tree(self, target, base_dir, repo, branch):
        target = Path(target)
        if target.exists():
            if (target / "odoo-bin").is_file() and not (target / ".git").exists():
                return False
            if (target / ".git").exists():
                raise CommandError(
                    "El árbol Odoo preexistente conserva metadatos Git; no serán eliminados automáticamente."
                )
            raise CommandError(
                "El directorio Odoo preexistente no contiene una instalación válida."
            )

        self._run_git(
            [
                "git", "clone", "--depth", "1", "--single-branch",
                "--branch", branch, repo, str(target),
            ],
            repo,
            timeout=1800,
        )
        if not (target / "odoo-bin").is_file():
            raise CommandError("El clone upstream no contiene odoo-bin.")

        base_resolved = Path(base_dir).resolve()
        target_resolved = target.resolve()
        if target_resolved.parent != base_resolved:
            raise CommandError("Ruta del clone Odoo no permitida.")
        for name in (".git", ".github"):
            metadata = target_resolved / name
            if metadata.exists() and metadata.parent == target_resolved:
                shutil.rmtree(metadata)
        return True

    def _prepare_custom_repo(self, repo, target):
        target = Path(target)
        branch = str(self.pcfg.get("custom_addons_branch") or "").strip()
        if not target.exists():
            self._validate_git_access(repo)
            cmd = ["git", "clone", "--single-branch"]
            if branch:
                cmd.extend(["--branch", branch])
            cmd.extend([repo, str(target)])
            self._run_git(cmd, repo)
            return "Repositorio de módulos clonado"

        if not (target / ".git").is_dir():
            raise CommandError(
                "El directorio custom preexistente no es un repositorio Git y no será reemplazado."
            )
        remote = self._run_git(
            ["git", "-C", str(target), "remote", "get-url", "origin"],
            repo,
        ).get("output", "")
        if self._normalize_repo_url(remote) != self._normalize_repo_url(repo):
            raise CommandError(
                "El directorio custom existente pertenece a otro repositorio."
            )

        self._validate_git_access(repo)
        fetch = ["git", "-C", str(target), "fetch", "--prune", "origin"]
        if branch:
            fetch.append(branch)
        self._run_git(fetch, repo)
        if branch:
            local = self._run_git(
                ["git", "-C", str(target), "show-ref", "--verify", f"refs/heads/{branch}"],
                repo,
                check=False,
            )
            if local.get("success"):
                self._run_git(["git", "-C", str(target), "checkout", branch], repo)
            else:
                self._run_git(
                    ["git", "-C", str(target), "checkout", "-b", branch, "--track", f"origin/{branch}"],
                    repo,
                )
            self._run_git(
                ["git", "-C", str(target), "merge", "--ff-only", f"origin/{branch}"],
                repo,
            )
        return "Repositorio de módulos validado y actualizado"

    @staticmethod
    def _run_postgres_sql_private(sql):
        command = [
            "runuser", "-u", "postgres", "--", "psql",
            "-v", "ON_ERROR_STOP=1", "-d", "postgres",
        ]
        try:
            process = subprocess.run(
                command,
                input=sql,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                "Timeout configurando el rol PostgreSQL."
            ) from exc
        if process.returncode != 0:
            output = str(process.stdout or "").strip()[-1200:]
            raise CommandError(
                "No fue posible configurar el rol PostgreSQL."
                + (f" Detalle: {output}" if output else "")
            )
        return True

    def _ensure_postgres_role(self, owner):
        owner = assert_owner(owner)
        password = self._safe_config_value(
            self.pcfg.get("postgres_default_password") or "odoo",
            "postgres_default_password",
        )
        quoted_owner = '"' + owner.replace('"', '""') + '"'
        literal_owner = owner.replace("'", "''")
        literal_password = password.replace("'", "''")
        sql = (
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles "
            f"WHERE rolname = '{literal_owner}') THEN "
            f"CREATE ROLE {quoted_owner} LOGIN CREATEDB CREATEROLE; "
            "END IF; END $$; "
            f"ALTER ROLE {quoted_owner} WITH LOGIN CREATEDB CREATEROLE "
            f"PASSWORD '{literal_password}';"
        )
        self._run_postgres_sql_private(sql)

    def _validate_postgres_auth(self, owner, database_name="postgres"):
        owner = assert_owner(owner)
        host = self._safe_config_value(
            self.pcfg.get("postgres_host") or "127.0.0.1", "postgres_host"
        )
        port = int(self.pcfg.get("postgres_port") or 5432)
        password = self._safe_config_value(
            self.pcfg.get("postgres_default_password") or "odoo",
            "postgres_default_password",
        )
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        result = run(
            [
                "psql", "-h", host, "-p", str(port), "-U", owner,
                "-d", str(database_name), "-Atc", "SELECT 1",
            ],
            check=False,
            timeout=30,
            env=env,
        )
        env.pop("PGPASSWORD", None)
        if not result.get("success") or result.get("output", "").strip() != "1":
            detail = str(result.get("output") or "").strip()[-800:]
            raise CommandError(
                "No fue posible autenticar el usuario PostgreSQL generado."
                + (f" Detalle: {detail}" if detail else "")
            )
        return True

    # ---------------------------------------------------------
    # GENERAR ODOO.CONF
    # ---------------------------------------------------------

    def _odoo_conf(
        self,
        payload,
        owner,
        addons,
        log_path,
    ):
        version = str(payload.get("version_odoo") or "19")
        interface = self._safe_config_value(
            payload.get("http_interface") or "127.0.0.1",
            "http_interface",
        )
        host = self._safe_config_value(
            self.pcfg.get("postgres_host") or "127.0.0.1", "postgres_host"
        )
        port = int(self.pcfg.get("postgres_port") or 5432)
        password = self._safe_config_value(
            self.pcfg.get("postgres_default_password") or "odoo",
            "postgres_default_password",
        )
        admin_passwd = self._safe_config_value(
            self.pcfg.get("admin_passwd") or "genesispos", "admin_passwd"
        )
        if version == "17":
            port_lines = [
                f"xmlrpc_port = {payload['http_port']}",
                f"longpolling_port = {payload['gevent_port']}",
                f"xmlrpc_interface = {interface}",
            ]
        else:
            port_lines = [
                f"http_port = {payload['http_port']}",
                f"gevent_port = {payload['gevent_port']}",
                f"http_interface = {interface}",
            ]

        database_name = str(payload.get("database_name") or "").strip()
        database_lines = [f"dbfilter = ^{database_name}$"] if database_name else []
        return "\n".join(
            [
                "[options]",
                f"admin_passwd = {admin_passwd}",
                f"db_host = {host}",
                f"db_port = {port}",
                f"db_user = {owner}",
                f"db_password = {password}",
                *database_lines,
                f"addons_path = {','.join(addons)}",
                *port_lines,
                f"workers = {payload['workers']}",
                f"max_cron_threads = {payload['max_cron_threads']}",
                f"proxy_mode = {bool(payload.get('proxy_mode', True))}",
                f"logfile = {log_path}",
                f"log_level = {payload.get('log_level') or 'warn'}",
                "list_db = True",
                "",
            ]
        )

    def _ensure_database_connection_config(self, path, owner):
        path = Path(path)
        if not path.is_file():
            raise CommandError(
                "No existe la configuración Odoo del servicio destino."
            )
        owner = assert_owner(owner)
        parser = configparser.RawConfigParser(interpolation=None, strict=False)
        try:
            with path.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error) as exc:
            raise CommandError(
                "No fue posible leer la configuración Odoo destino."
            ) from exc
        if not parser.has_section("options"):
            raise CommandError("La configuración Odoo no contiene [options].")

        parser.set(
            "options",
            "db_host",
            self._safe_config_value(
                self.pcfg.get("postgres_host") or "127.0.0.1",
                "postgres_host",
            ),
        )
        parser.set("options", "db_port", str(int(self.pcfg.get("postgres_port") or 5432)))
        parser.set("options", "db_user", owner)
        parser.set(
            "options",
            "db_password",
            self._safe_config_value(
                self.pcfg.get("postgres_default_password") or "odoo",
                "postgres_default_password",
            ),
        )

        current_stat = path.stat()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                parser.write(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(current_stat.st_mode))
            os.chown(temporary, current_stat.st_uid, current_stat.st_gid)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def _validate_odoo_conf(self, path, payload):
        parser = configparser.RawConfigParser(interpolation=None, strict=False)
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error) as exc:
            raise CommandError("No fue posible validar el archivo Odoo.") from exc
        if not parser.has_section("options"):
            raise CommandError("La configuración Odoo no contiene [options].")
        required = [
            "admin_passwd", "db_host", "db_port", "db_user", "db_password",
            "addons_path", "workers", "proxy_mode", "logfile", "log_level",
        ]
        if str(payload.get("version_odoo") or "19") == "17":
            required.extend(["xmlrpc_port", "longpolling_port", "xmlrpc_interface"])
        else:
            required.extend(["http_port", "gevent_port", "http_interface"])
        missing = [
            key for key in required
            if not parser.has_option("options", key)
            or not parser.get("options", key).strip()
        ]
        if missing:
            raise CommandError(
                "La configuración Odoo está incompleta: " + ", ".join(missing)
            )
        if parser.get("options", "db_password").strip().lower() in ("false", "none"):
            raise CommandError("db_password no puede estar vacío con conexión TCP.")
        return True

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