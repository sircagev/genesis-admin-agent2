"""Configuración segura y tipada del remoto rclone administrado por Genesis."""
import configparser
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .client import ControllerClient
from .commands import CommandError

REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class RcloneConfigurator:
    def __init__(self, config, client=None, progress_callback=None):
        self.config = config
        self.client = client or ControllerClient(config)
        self.progress_callback = progress_callback

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

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

    def _ensure_rclone(self):
        """Instala rclone solo cuando no está disponible en el Agent."""
        if shutil.which("rclone"):
            self._progress("rclone", 25, "rclone ya está instalado.")
            return False
        if os.geteuid() != 0:
            raise CommandError(
                "rclone no está instalado y el Agent no tiene permisos root para instalarlo."
            )
        apt_get = shutil.which("apt-get")
        if not apt_get:
            raise CommandError(
                "rclone no está instalado y este sistema no dispone de apt-get."
            )

        self._progress("rclone_install", 15, "rclone no está instalado; actualizando índices APT.")
        for command, message in (
            ([apt_get, "update"], "No fue posible actualizar los índices APT para instalar rclone."),
            ([apt_get, "install", "-y", "rclone"], "No fue posible instalar rclone."),
        ):
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if result.returncode:
                raise CommandError(message)
        if not shutil.which("rclone"):
            raise CommandError("rclone no quedó disponible después de la instalación.")
        self._progress("rclone_install", 40, "rclone instalado correctamente.")
        return True

    def configure(self):
        rclone_installed = self._ensure_rclone()
        self._progress("rclone", 55, "Obteniendo configuración segura de rclone.")
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
        self._progress("rclone", 80, "Validando remoto rclone.")
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
            "rclone_installed": rclone_installed,
            "config_path": str(path),
            "message": "Remoto rclone configurado, validado y sincronizado con backups.",
        }
