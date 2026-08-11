import os
import re
import secrets
import shutil
from pathlib import Path

from .commands import (
    CommandError,
    assert_owner,
    port_available,
    run,
    systemd_exists,
)

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


class OdooProvisioner:
    def __init__(self, config):
        self.config = config
        self.pcfg = config.section("provision")

    def owner_from_unit(self, unit):
        prefix = "odoo-server-"
        unit = str(unit or "").strip()
        if unit.endswith(".service"):
            unit = unit[:-8]
        if not unit.startswith(prefix):
            raise CommandError("La unidad debe iniciar con odoo-server-")
        return assert_owner(unit[len(prefix):])

    def validate_payload(self, payload):
        unit = payload["service_name"]
        owner = self.owner_from_unit(unit)
        version = str(payload.get("version_odoo") or "19")
        if version not in {"17", "18", "19"}:
            raise CommandError("Versión Odoo permitida: 17, 18 o 19.")

        http_port = int(payload["http_port"])
        gevent_port = int(payload["gevent_port"])
        if http_port == gevent_port:
            raise CommandError("http_port y gevent_port deben ser distintos.")

        domain = (payload.get("domain") or "").strip().lower()
        if not DOMAIN_RE.fullmatch(domain):
            raise CommandError(f"Dominio inválido: {domain!r}")

        return {
            **payload,
            "owner": owner,
            "version_odoo": version,
            "http_port": http_port,
            "gevent_port": gevent_port,
            "workers": int(payload.get("workers") or 0),
            "max_cron_threads": int(payload.get("max_cron_threads") or 0),
            "domain": domain,
        }

    def prepare(self, raw_payload):
        payload = self.validate_payload(raw_payload)
        checks = {
            "service_not_exists": not systemd_exists(payload["service_name"]),
            "http_port_available": port_available(payload["http_port"]),
            "gevent_port_available": port_available(payload["gevent_port"]),
            "nginx_installed": shutil.which("nginx") is not None,
            "postgresql_ok": run(
                ["runuser", "-u", "postgres", "--", "psql", "-Atc", "SELECT 1;"],
                check=False,
            )["success"],
        }
        if payload.get("create_ssl"):
            checks["certbot_installed"] = shutil.which("certbot") is not None
        else:
            checks["certbot_installed"] = True

        failed = [key for key, value in checks.items() if not value]
        return {
            "success": not failed,
            "checks": checks,
            "failed_checks": failed,
            "normalized": payload,
        }

    def create(self, raw_payload):
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
        base_dir = Path(self.pcfg.get("base_dir") or "/opt") / owner
        odoo_dir = base_dir / "odoo-server"
        venv = odoo_dir / "venv"
        custom_dir = odoo_dir / f"modulosFE{payload['version_odoo']}"
        conf_path = Path(f"/etc/odoo-{owner}.conf")
        log_path = Path(
            payload.get("logfile")
            or f"/var/log/odoo/odoo-server-{owner}.log"
        )
        service_name = str(payload["service_name"]).strip()
        if service_name.endswith(".service"):
            systemd_unit = service_name
        else:
            systemd_unit = f"{service_name}.service"
        systemd_path = Path(
            f"/etc/systemd/system/{systemd_unit}"
        )
        nginx_path = Path(f"/etc/nginx/sites-available/{owner}.conf")
        nginx_link = Path(f"/etc/nginx/sites-enabled/{owner}.conf")

        steps = []

        if not self._linux_user_exists(owner):
            run(
                [
                    "useradd",
                    "--system",
                    "--create-home",
                    "--home-dir",
                    str(base_dir),
                    "--shell",
                    "/bin/bash",
                    owner,
                ]
            )
            steps.append("Usuario Linux creado")

        base_dir.mkdir(parents=True, exist_ok=True)

        if not (odoo_dir / ".git").exists():
            run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    f"{payload['version_odoo']}.0",
                    self.pcfg.get("odoo_repo") or "https://github.com/odoo/odoo.git",
                    str(odoo_dir),
                ],
                timeout=1800,
            )
            steps.append("Odoo clonado")

        if not venv.exists():
            run(["python3", "-m", "venv", str(venv)], timeout=120)
            run([str(venv / "bin/pip"), "install", "--upgrade", "pip", "wheel"], timeout=600)
            run(
                [
                    str(venv / "bin/pip"),
                    "install",
                    "-r",
                    str(odoo_dir / "requirements.txt"),
                ],
                timeout=1800,
            )
            steps.append("Virtualenv y requirements instalados")

        custom_repo = (self.pcfg.get("custom_addons_repo") or "").strip()
        if custom_repo and not (custom_dir / ".git").exists():
            self._clone_custom_repo(custom_repo, custom_dir)
            steps.append("Repositorio de módulos clonado")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)

        self._ensure_postgres_role(owner)
        steps.append("Rol PostgreSQL verificado")

        addons = [
            str(odoo_dir / "odoo/addons"),
            str(odoo_dir / "addons"),
        ]
        if custom_dir.exists():
            subpaths = (
                self.pcfg.get("custom_addons_subpaths")
                or ["custom_addons", "modulos"]
            )
            found_custom = False
            for subpath in subpaths:
                candidate = custom_dir / str(subpath)
                if candidate.exists():
                    addons.append(str(candidate))
                    found_custom = True
            if not found_custom:
                addons.append(str(custom_dir))

        conf_path.write_text(
            self._odoo_conf(payload, owner, addons, log_path),
            encoding="utf-8",
        )
        os.chmod(conf_path, 0o640)

        systemd_path.write_text(
            self._systemd_unit(payload, owner, odoo_dir, venv, conf_path),
            encoding="utf-8",
        )
        steps.append("Configuración Odoo y systemd creadas")

        if payload.get("create_nginx", True):
            nginx_path.write_text(self._nginx_conf(payload, owner), encoding="utf-8")
            if not nginx_link.exists():
                nginx_link.symlink_to(nginx_path)
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
            steps.append("Nginx configurado")

        run(["chown", "-R", f"{owner}:{owner}", str(base_dir)])
        run(["chown", f"{owner}:{owner}", str(log_path)])
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", payload["service_name"]])

        if payload.get("start_service", True):
            run(["systemctl", "restart", payload["service_name"]], timeout=120)
            steps.append("Servicio Odoo iniciado")

        if payload.get("create_ssl", True):
            email = (
                payload.get("certbot_email")
                or self.pcfg.get("certbot_email")
                or ""
            ).strip()
            if not email:
                raise CommandError(
                    "create_ssl está activo pero no hay certbot_email configurado."
                )
            run(
                [
                    "certbot",
                    "--nginx",
                    "-d",
                    payload["domain"],
                    "--non-interactive",
                    "--agree-tos",
                    "--redirect",
                    "-m",
                    email,
                ],
                timeout=600,
            )
            steps.append("Certificado SSL instalado")

        return {
            "success": True,
            "dry_run": False,
            "message": "Instancia Odoo creada.",
            "owner": owner,
            "unit": payload["service_name"],
            "base_dir": str(base_dir),
            "config": str(conf_path),
            "steps": steps,
        }

    def _plan(self, payload):
        owner = payload["owner"]
        return {
            "owner": owner,
            "unit": payload["service_name"],
            "odoo_branch": f"{payload['version_odoo']}.0",
            "base_dir": f"{self.pcfg.get('base_dir') or '/opt'}/{owner}",
            "http_port": payload["http_port"],
            "gevent_port": payload["gevent_port"],
            "workers": payload["workers"],
            "max_cron_threads": payload["max_cron_threads"],
            "domain": payload["domain"],
            "create_nginx": payload.get("create_nginx", True),
            "create_ssl": payload.get("create_ssl", True),
        }

    @staticmethod
    def _linux_user_exists(owner):
        return run(["id", owner], check=False)["success"]

    @staticmethod
    def _ensure_postgres_role(owner):
        sql = (
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{owner}') THEN "
            f"CREATE ROLE \"{owner}\" LOGIN CREATEDB; "
            "END IF; END $$;"
        )
        run(["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql])

    def _clone_custom_repo(self, repo, target):
        env = os.environ.copy()
        token = (env.get("GITHUB_TOKEN") or "").strip()
        branch = (self.pcfg.get("custom_addons_branch") or "").strip()
        cmd = ["git", "clone"]
        if branch:
            cmd += ["--branch", branch]
        if token and repo.startswith("https://github.com/"):
            askpass = Path("/tmp/genesis-agent-git-askpass.sh")
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Username*) echo "x-access-token" ;;\n'
                '  *Password*) echo "$GITHUB_TOKEN" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
            try:
                run(cmd + [repo, str(target)], timeout=1200, env=env)
            finally:
                askpass.unlink(missing_ok=True)
        else:
            run(cmd + [repo, str(target)], timeout=1200, env=env)

    @staticmethod
    def _odoo_conf(payload, owner, addons, log_path):
        return "\n".join(
            [
                "[options]",
                f"admin_passwd = {secrets.token_urlsafe(32)}",
                f"db_user = {owner}",
                "db_password = False",
                f"addons_path = {','.join(addons)}",
                f"http_port = {payload['http_port']}",
                f"gevent_port = {payload['gevent_port']}",
                f"workers = {payload['workers']}",
                f"max_cron_threads = {payload['max_cron_threads']}",
                f"http_interface = {payload.get('http_interface') or '127.0.0.1'}",
                f"proxy_mode = {bool(payload.get('proxy_mode', True))}",
                f"logfile = {log_path}",
                f"log_level = {payload.get('log_level') or 'info'}",
                "list_db = True",
                "",
            ]
        )

    @staticmethod
    def _systemd_unit(payload, owner, odoo_dir, venv, conf_path):
        return f"""[Unit]
Description=Odoo {owner}
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={owner}
Group={owner}
WorkingDirectory={odoo_dir}
ExecStart={venv}/bin/python {odoo_dir}/odoo-bin -c {conf_path}
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""

    @staticmethod
    def _nginx_conf(payload, owner):
        upstream = "odoo_" + owner.replace("-", "_")
        chat = upstream + "_chat"
        domain = payload["domain"]
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
