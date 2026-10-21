"""Backups locales programados de bases Odoo descubiertas por el Agent."""
import datetime
import fcntl
import os
import pwd
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .commands import CommandError
from .database_manager import DatabaseManager
from .discovery import OdooServiceDiscovery


FREQUENCIES = ("hourly", "daily", "weekly")


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class ScheduledBackupManager:
    """Operación tipada: descubre, genera, verifica y conserva backups."""

    def __init__(self, config, discovery=None, database_manager=None):
        self.config = config
        self.settings = config.section("scheduled_backup")
        self.discovery = discovery or OdooServiceDiscovery(config)
        self.database_manager = database_manager or DatabaseManager(config)

    def _setting(self, name, default=""):
        return self.settings.get(name, default)

    def _retention_days(self, frequency):
        policy = (self._setting("retention", {}) or {}).get(frequency) or {}
        try:
            days = int(policy.get("days"))
        except (TypeError, ValueError) as exc:
            raise CommandError(f"Retención inválida para {frequency}.") from exc
        if days < 1:
            raise CommandError(f"Retención inválida para {frequency}.")
        return days

    def _backup_dir(self):
        path = Path(str(self._setting("backup_dir", "") or "").strip())
        if not path.is_absolute() or path == Path("/"):
            raise CommandError("scheduled_backup.backup_dir debe ser una ruta absoluta segura.")
        return path

    def _rclone_remote(self):
        remote = str(self._setting("rclone_remote", "") or "").strip().rstrip("/")
        if not remote or ":" not in remote:
            raise CommandError("scheduled_backup.rclone_remote no es válido.")
        return remote

    def _ensure_remote(self, remote):
        name = remote.split(":", 1)[0] + ":"
        result = subprocess.run(
            ["rclone", "listremotes"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=False,
        )
        if result.returncode != 0 or name not in set(result.stdout.splitlines()):
            raise CommandError(f"El remoto rclone configurado no existe: {name}")

    def discover_targets(self):
        """Selecciona solo BDs asociadas inequívocamente a servicios activos."""
        services = [
            item for item in (self.discovery.discover().get("services") or [])
            if item.get("active_state") == "active"
        ]
        by_unit = {item.get("unit"): item for item in services if item.get("unit")}
        inventory = self.discovery.discover_databases(services=services)
        if not inventory.get("success"):
            raise CommandError(
                "No fue posible consultar PostgreSQL: "
                f"{inventory.get('error') or inventory.get('message') or ''}"
            )
        by_name = {
            item.get("name"): item for item in (inventory.get("databases") or [])
            if item.get("name")
        }
        configured = [str(item).strip() for item in self._setting("database_names", []) if str(item).strip()]
        names = configured or sorted(by_name)
        manual = bool(configured)
        targets, failures, skipped = [], [], []
        for name in names:
            database = by_name.get(name)
            if database is None:
                (failures if manual else skipped).append(f"{name}: no existe en PostgreSQL")
                continue
            if database.get("association_state") != "matched":
                (failures if manual else skipped).append(
                    f"{name}: asociación {database.get('association_state') or 'unknown'}"
                )
                continue
            unit = database.get("service_unit")
            service = by_unit.get(unit)
            if service is None:
                (failures if manual else skipped).append(f"{name}: servicio no activo")
                continue
            try:
                runtime = self.database_manager.service_odoo_runtime(
                    unit, service.get("config_path") or ""
                )
                system_user = database.get("system_user") or service.get("user") or "odoo"
                pwd.getpwnam(system_user)
            except Exception as exc:  # continuar con las demás bases
                failures.append(f"{name}: runtime inválido: {exc}")
                continue
            targets.append({
                "database": name, "unit": unit, "system_user": system_user,
                "python": str(runtime["python"]), "odoo_bin": str(runtime["odoo_bin"]),
                "config": str(runtime["config"]),
                "association_method": database.get("association_method") or "",
            })
        return targets, failures, skipped, len(services)

    @staticmethod
    def _dump_command(target):
        command = [target["python"], target["odoo_bin"], "db", "-c", target["config"],
                   "dump", target["database"], "-"]
        user = target["system_user"]
        if os.geteuid() == 0:
            return ["runuser", "-u", user, "--", *command]
        if pwd.getpwuid(os.geteuid()).pw_name != user:
            raise PermissionError(f"Ejecute como root o como {user}.")
        return command

    @staticmethod
    def _validate_zip(path):
        with zipfile.ZipFile(path) as archive:
            if not {"dump.sql", "manifest.json"}.issubset(archive.namelist()):
                raise CommandError("ZIP incompleto: falta dump.sql o manifest.json.")
            if archive.getinfo("dump.sql").file_size == 0:
                raise CommandError("dump.sql vacío.")
            bad_file = archive.testzip()
            if bad_file is not None:
                raise CommandError(f"CRC inválido en {bad_file}.")

    def _cleanup(self, path, database, frequency, remote):
        cutoff = _utcnow() - datetime.timedelta(days=self._retention_days(frequency))
        for old in path.iterdir():
            try:
                timestamp = old.name.removeprefix(f"{database}_").split(".")[0]
                if old.is_file() and datetime.datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S") < cutoff:
                    old.unlink()
            except (OSError, ValueError):
                continue
        result = subprocess.run(["rclone", "lsf", remote], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False)
        if result.returncode:
            return
        for name in result.stdout.splitlines():
            try:
                timestamp = name.removeprefix(f"{database}_").split(".")[0]
                if name.endswith(".zip") and datetime.datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S") < cutoff:
                    subprocess.run(["rclone", "delete", remote + name], check=True)
            except (subprocess.SubprocessError, ValueError):
                continue

    def _backup_target(self, target, frequency, base_dir, remote_root):
        database = target["database"]
        timestamp = _utcnow().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{database}_{timestamp}.zip"
        directory = base_dir / database / frequency
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        remote = f"{remote_root}/{os.uname().nodename}/{database}/{frequency}/"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{name}.", suffix=".part",
                                             dir=directory, delete=False) as output:
                temporary = Path(output.name)
                subprocess.run(self._dump_command(target), stdout=output, check=True)
                output.flush()
                os.fsync(output.fileno())
            self._validate_zip(temporary)
            os.replace(temporary, destination)
            temporary = None
            subprocess.run(["rclone", "copy", str(destination), remote, "--stats", "30s"], check=True)
            subprocess.run(["rclone", "check", str(directory), remote, "--one-way",
                            "--include", f"/{name}"], check=True)
            self._cleanup(directory, database, frequency, remote)
            return {"database": database, "success": True, "file": str(destination),
                    "size": destination.stat().st_size}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def run(self, frequency="daily", dry_run=False, force=False, progress_callback=None):
        if frequency not in FREQUENCIES:
            raise CommandError("Frecuencia inválida.")
        if not dry_run and not force and not self._setting("enabled", False):
            raise CommandError("scheduled_backup está desactivado en config.yaml.")
        targets, failures, skipped, service_count = self.discover_targets()
        if dry_run:
            return {"success": not failures, "dry_run": True, "services": service_count,
                    "targets": targets, "skipped": skipped, "failures": failures,
                    "message": f"Dry-run: {len(targets)} bases seleccionadas; {len(skipped)} omitidas."}
        base_dir, remote = self._backup_dir(), self._rclone_remote()
        base_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_remote(remote)
        results = []
        with (base_dir / ".backup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for index, target in enumerate(targets, start=1):
                if callable(progress_callback):
                    progress_callback("backup", int(index * 100 / max(len(targets), 1)),
                                      f"Respaldando {target['database']} ({index}/{len(targets)}).")
                try:
                    results.append(self._backup_target(target, frequency, base_dir, remote))
                except Exception as exc:  # el lote continúa
                    failures.append(f"{target['database']}: {exc}")
        return {"success": not failures, "frequency": frequency, "services": service_count,
                "selected": len(targets), "skipped": skipped, "results": results,
                "failures": failures,
                "message": f"Backup {frequency}: {len(results)}/{len(targets)} bases completadas."}
