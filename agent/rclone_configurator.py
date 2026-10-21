"""Configuración segura y tipada del remoto rclone administrado por Genesis."""
import configparser
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .client import ControllerClient
from .commands import CommandError

REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class RcloneConfigurator:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or ControllerClient(config)

    @staticmethod
    def _validate(values):
        remote = str(values.get("remote_name") or "").strip()
        if not REMOTE_RE.fullmatch(remote):
            raise CommandError("Nombre de remoto rclone inválido.")
        remote_path = str(values.get("remote_path") or "").strip().strip("/")
        if not remote_path or any(character in remote_path for character in ("\x00", "\n", "\r")):
            raise CommandError("Ruta de remoto rclone inválida.")
        token = str(values.get("token") or "").strip()
        if not token:
            raise CommandError("El Controller no entregó un token rclone.")
        return remote, remote_path, token

    @staticmethod
    def _path():
        return Path("/root/.config/rclone/rclone.conf")

    def configure(self):
        values = self.client.rclone_config()
        if not values.get("success"):
            raise CommandError(values.get("message") or "No se obtuvo configuración rclone.")
        remote, remote_path, token = self._validate(values)
        destination = f"{remote}:{remote_path}"
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = path.read_bytes() if path.exists() else None
        previous_mode = path.stat().st_mode & 0o777 if path.exists() else None
        parser = configparser.RawConfigParser()
        parser.optionxform = str
        if path.exists():
            parser.read(path, encoding="utf-8")
        if not parser.has_section(remote):
            parser.add_section(remote)
        parser.set(remote, "type", "drive")
        for key in ("client_id", "client_secret"):
            value = str(values.get(key) or "").strip()
            if value:
                parser.set(remote, key, value)
            elif parser.has_option(remote, key):
                parser.remove_option(remote, key)
        parser.set(remote, "token", token)
        descriptor, temporary = tempfile.mkstemp(prefix="rclone-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                parser.write(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        result = subprocess.run(["rclone", "lsd", destination], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False)
        if result.returncode:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
                os.chmod(path, previous_mode)
            raise CommandError("rclone no pudo validar el remoto configurado.")
        backup = self.config.data.setdefault("scheduled_backup", {})
        backup["rclone_remote"] = destination
        backup["enabled"] = bool(values.get("backup_enabled", False))
        self.config.save()
        return {
            "success": True,
            "remote_name": remote,
            "remote_path": remote_path,
            "backup_remote": destination,
            "backup_enabled": backup["enabled"],
            "config_path": str(path),
            "message": "Remoto rclone configurado, validado y sincronizado con backups.",
        }
