import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_CONFIG = Path("/opt/genesis-admin-agent/config/config.yaml")

DEFAULTS = {
    "controller_url": "",
    "database": "",
    "server_code": "",
    "enrollment_token": "",
    "agent_id": "",
    "agent_token": "",
    "verify_tls": True,
    "poll_interval": 3,
    "heartbeat_interval": 30,
    "service_sync_interval": 300,
    "request_timeout": 30,
    "log_default_lines": 200,
    "allowed_exact": ["nginx", "remote_print"],
    "allowed_prefix": ["odoo-server-"],
    "provision": {
        "base_dir": "/opt",
        "odoo_repo": "https://github.com/odoo/odoo.git",
        "odoo_branch": "19.0",
        "admin_passwd": "genesispos",
        "postgres_host": "127.0.0.1",
        "postgres_port": 5432,
        "postgres_default_password": "odoo",
        "postgres_validate_auth": True,
        "custom_addons_repo": "https://github.com/evopos30/modulosFE19.git",
        "custom_addons_branch": "",
        "github_auth_enabled": False,
        "github_username": "",
        "custom_addons_subpaths": ["custom_addons", "modulos"],
        "certbot_email": "",
        "default_http_interface": "127.0.0.1",
        "default_log_level": "info",
    },
}


def _deep_merge(defaults, current):
    result = deepcopy(defaults)
    if not isinstance(current, dict):
        return result
    for key, value in current.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        elif key in result:
            result[key] = value
    return result


class AgentConfig:
    def __init__(self, path=None):
        self.path = Path(
            path or os.getenv("GENESIS_AGENT_CONFIG") or DEFAULT_CONFIG
        )
        self.data = {}
        self.reload()

    def reload(self):
        if not self.path.exists():
            raise RuntimeError(
                f"No existe la configuración del agente: {self.path}"
            )
        with self.path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        self.data = _deep_merge(DEFAULTS, raw)
        self.validate()

    def validate(self):
        controller = str(self.data.get("controller_url") or "").strip()
        parsed = urlparse(controller)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(
                "controller_url debe ser una URL HTTP/HTTPS válida."
            )
        if not str(self.data.get("database") or "").strip():
            raise RuntimeError("Falta database en config.yaml.")
        if not str(self.data.get("server_code") or "").strip():
            raise RuntimeError("Falta server_code en config.yaml.")
        if not isinstance(self.data.get("allowed_exact"), list):
            raise RuntimeError("allowed_exact debe ser una lista YAML.")
        if not isinstance(self.data.get("allowed_prefix"), list):
            raise RuntimeError("allowed_prefix debe ser una lista YAML.")
        if not isinstance(self.data.get("provision"), dict):
            raise RuntimeError("provision debe ser un objeto YAML.")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def section(self, key):
        value = self.data.get(key)
        return value if isinstance(value, dict) else {}

    def is_enrolled(self):
        return bool(
            str(self.data.get("agent_id") or "").strip()
            and str(self.data.get("agent_token") or "").strip()
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(
                self.data,
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def save_identity(self, agent_id, agent_token):
        self.data["agent_id"] = str(agent_id or "")
        self.data["agent_token"] = str(agent_token or "")
        self.data["enrollment_token"] = ""
        self.save()

    def prepare_reenrollment(
        self,
        token,
        controller_url=None,
        database=None,
        server_code=None,
    ):
        token = str(token or "").strip()
        if not token:
            raise RuntimeError("El token de enrolamiento no puede estar vacío.")

        if controller_url:
            self.data["controller_url"] = str(controller_url).strip().rstrip("/")
        if database:
            self.data["database"] = str(database).strip()
        if server_code:
            self.data["server_code"] = str(server_code).strip()

        self.data["enrollment_token"] = token
        self.data["agent_id"] = ""
        self.data["agent_token"] = ""
        self.validate()
        self.save()
