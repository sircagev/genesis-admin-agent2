import base64
import binascii
import grp
import os
import pwd
import re
import shutil
import tempfile
from pathlib import Path

from .bootstrap import ServerBootstrapAuditor
from .commands import CommandError, run


BASE_PACKAGES = (
    "git",
    "wget",
    "curl",
    "ca-certificates",
    "gnupg",
    "python3",
    "build-essential",
    "libzip-dev",
    "python3-dev",
    "libxslt1-dev",
    "python3-pip",
    "libldap2-dev",
    "python3-wheel",
    "libsasl2-dev",
    "python3-venv",
    "python3-setuptools",
    "libjpeg-dev",
    "xfonts-75dpi",
    "xfonts-base",
    "libxrender1",
    "libpq-dev",
    "libffi-dev",
    "fontconfig",
    "libcairo2",
    "libcairo2-dev",
    "libfreetype6-dev",
    "libpng-dev",
    "zlib1g-dev",
    "postgresql",
    "nginx",
    "certbot",
    "python3-certbot-nginx",
)

SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}

SSH_PROFILE = Path("/etc/ssh/sshd_config.d/40-genesispos.conf")
SSH_SOCKET_PROFILE = Path("/etc/systemd/system/ssh.socket.d/40-genesispos.conf")


class ServerConfigurator:
    """Idempotent, typed server installation and two-phase SSH hardening."""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self.runtime_config = {}

    def set_runtime_config(self, values):
        self.runtime_config = dict(values or {})

    def clear_runtime_config(self):
        self.runtime_config = {}

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

    @staticmethod
    def _require_root():
        if os.geteuid() != 0:
            raise CommandError(
                "La configuración del servidor requiere que el Agent se ejecute como root."
            )

    @staticmethod
    def _atomic_write(path, content, mode=0o644):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        ownership = None
        if path.exists():
            current = path.stat()
            ownership = (current.st_uid, current.st_gid)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            if ownership:
                os.chown(temporary, *ownership)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _node_major():
        executable = shutil.which("node")
        if not executable:
            return 0
        result = run([executable, "--version"], check=False, timeout=20)
        match = re.search(r"(\d+)", str(result.get("output") or ""))
        return int(match.group(1)) if match else 0

    def _ensure_node_20(self):
        if self._node_major() == 20:
            return
        self._progress("nodejs", 42, "Configurando el repositorio de Node.js 20.")
        with tempfile.TemporaryDirectory(prefix="genesis-node-") as temp_dir:
            key_file = Path(temp_dir) / "nodesource.asc"
            run(
                [
                    "curl",
                    "-fsSL",
                    "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key",
                    "-o",
                    str(key_file),
                ],
                timeout=120,
            )
            run(
                [
                    "gpg",
                    "--dearmor",
                    "--yes",
                    "--output",
                    "/usr/share/keyrings/nodesource.gpg",
                    str(key_file),
                ],
                timeout=60,
            )
        architecture = run(
            ["dpkg", "--print-architecture"], timeout=20
        ).get("output")
        self._atomic_write(
            "/etc/apt/sources.list.d/nodesource.list",
            (
                f"deb [arch={architecture} signed-by=/usr/share/keyrings/nodesource.gpg] "
                "https://deb.nodesource.com/node_20.x nodistro main\n"
            ),
        )
        run(["apt-get", "update"], timeout=900)
        run(["apt-get", "install", "-y", "nodejs"], timeout=900)
        if self._node_major() != 20:
            raise CommandError("Node.js 20 no quedó instalado correctamente.")

    def _ensure_wkhtmltopdf(self):
        executable = shutil.which("wkhtmltopdf")
        if executable:
            version = run(
                [executable, "--version"], check=False, timeout=20
            ).get("output")
            if "0.12.6.1" in str(version or ""):
                return
        architecture = str(
            run(["dpkg", "--print-architecture"], timeout=20).get("output") or ""
        ).strip()
        if architecture not in ("amd64", "arm64"):
            raise CommandError(
                f"No existe paquete wkhtmltopdf administrado para {architecture}."
            )
        package_name = f"wkhtmltox_0.12.6.1-2.jammy_{architecture}.deb"
        url = (
            "https://github.com/wkhtmltopdf/packaging/releases/download/"
            f"0.12.6.1-2/{package_name}"
        )
        self._progress("wkhtmltopdf", 56, "Instalando wkhtmltopdf 0.12.6.1.")
        with tempfile.TemporaryDirectory(prefix="genesis-wkhtml-") as temp_dir:
            package_path = Path(temp_dir) / package_name
            run(["curl", "-fL", url, "-o", str(package_path)], timeout=300)
            run(
                ["apt-get", "install", "-y", str(package_path)],
                timeout=900,
            )
        version = run(
            ["wkhtmltopdf", "--version"], check=False, timeout=20
        ).get("output")
        if "0.12.6.1" not in str(version or ""):
            raise CommandError("wkhtmltopdf 0.12.6.1 no quedó instalado.")

    @staticmethod
    def _ensure_odoo_user():
        try:
            account = pwd.getpwnam("odoo")
            group = grp.getgrgid(account.pw_gid)
        except KeyError:
            run(
                [
                    "useradd",
                    "--system",
                    "--user-group",
                    "--home-dir",
                    "/opt/odoo",
                    "--create-home",
                    "--shell",
                    "/bin/bash",
                    "odoo",
                ],
                timeout=60,
            )
            account = pwd.getpwnam("odoo")
            group = grp.getgrgid(account.pw_gid)
        if group.gr_name != "odoo":
            raise CommandError(
                "El usuario odoo existe, pero su grupo primario no es odoo."
            )
        Path("/opt/odoo").mkdir(parents=True, exist_ok=True)
        os.chown("/opt/odoo", account.pw_uid, account.pw_gid)

    def _ensure_postgresql_scram(self):
        query = run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-At",
                "-d",
                "postgres",
                "-c",
                "SHOW hba_file;",
            ],
            timeout=30,
        )
        hba_file = Path(str(query.get("output") or "").splitlines()[-1])
        if not hba_file.is_file():
            raise CommandError("PostgreSQL no informó un pg_hba.conf válido.")
        changed = False
        output = []
        has_ipv4 = False
        has_ipv6 = False
        for original in hba_file.read_text(encoding="utf-8").splitlines():
            line = original
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                parts = stripped.split()
                if len(parts) >= 5 and parts[0].startswith("host"):
                    address = parts[3]
                    if address in ("127.0.0.1/32", "::1/128"):
                        if address == "127.0.0.1/32":
                            has_ipv4 = True
                        else:
                            has_ipv6 = True
                        if parts[-1] != "scram-sha-256":
                            parts[-1] = "scram-sha-256"
                            line = "    ".join(parts)
                            changed = True
            output.append(line)
        if not has_ipv4:
            output.append("host    all    all    127.0.0.1/32    scram-sha-256")
            changed = True
        if not has_ipv6:
            output.append("host    all    all    ::1/128    scram-sha-256")
            changed = True
        if changed:
            self._atomic_write(hba_file, "\n".join(output) + "\n", mode=0o640)
        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-d",
                "postgres",
                "-c",
                "ALTER SYSTEM SET password_encryption = 'scram-sha-256';",
            ],
            timeout=30,
        )
        run(["systemctl", "reload", "postgresql.service"], timeout=120)
        run(["pg_isready", "-h", "127.0.0.1"], timeout=30)

    def install(self, _payload=None):
        self._require_root()
        self._progress("packages", 10, "Actualizando el índice de paquetes.")
        run(["apt-get", "update"], timeout=900)
        self._progress("packages", 22, "Instalando dependencias base de Odoo.")
        run(["apt-get", "install", "-y", *BASE_PACKAGES], timeout=1800)
        self._ensure_node_20()
        self._progress("rtlcss", 49, "Instalando rtlcss.")
        run(["npm", "install", "-g", "rtlcss"], timeout=600)
        self._ensure_wkhtmltopdf()
        self._progress("odoo_user", 66, "Verificando el usuario Linux odoo.")
        self._ensure_odoo_user()
        self._progress("services", 74, "Habilitando PostgreSQL y Nginx.")
        run(["systemctl", "enable", "--now", "postgresql.service"], timeout=180)
        run(["systemctl", "enable", "--now", "nginx.service"], timeout=180)
        self._progress("postgresql", 82, "Aplicando autenticación SCRAM local.")
        self._ensure_postgresql_scram()
        self._progress("verification", 92, "Verificando la instalación.")
        result = ServerBootstrapAuditor(
            progress_callback=self.progress_callback
        ).audit({})
        result["installed"] = True
        result["apt_upgrade_executed"] = False
        result["postgres_role_created"] = False
        result["message"] = (
            "Dependencias del servidor instaladas y verificadas."
            if result.get("ready")
            else "La instalación terminó, pero la auditoría detectó pendientes."
        )
        return result

    @staticmethod
    def _parse_authorized_keys(value):
        keys = []
        for line_number, raw_line in enumerate(str(value or "").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
                raise CommandError(
                    f"Clave pública SSH inválida en la línea {line_number}."
                )
            encoded = parts[1]
            try:
                base64.b64decode(encoded + ("=" * (-len(encoded) % 4)), validate=True)
            except (binascii.Error, ValueError, TypeError) as exc:
                raise CommandError(
                    f"Clave pública SSH inválida en la línea {line_number}."
                ) from exc
            keys.append(line)
        if not keys:
            raise CommandError("Configure al menos una clave pública SSH.")
        return list(dict.fromkeys(keys))

    @staticmethod
    def _current_ssh_ports():
        result = run(["sshd", "-T"], timeout=30)
        ports = {
            int(match.group(1))
            for line in str(result.get("output") or "").splitlines()
            if (match := re.fullmatch(r"port\s+(\d+)", line.strip()))
        }
        return sorted(ports or {22})

    @staticmethod
    def _validate_port(value):
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise CommandError("El puerto SSH configurado no es válido.") from exc
        if not 1 <= port <= 65535:
            raise CommandError("El puerto SSH configurado no es válido.")
        return port

    @staticmethod
    def _install_root_keys(keys):
        ssh_dir = Path("/root/.ssh")
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        authorized = ssh_dir / "authorized_keys"
        existing = (
            authorized.read_text(encoding="utf-8").splitlines()
            if authorized.exists()
            else []
        )
        merged = [line for line in existing if line.strip()]
        for key in keys:
            if key not in merged:
                merged.append(key)
        ServerConfigurator._atomic_write(
            authorized, "\n".join(merged) + "\n", mode=0o600
        )
        os.chown(authorized, 0, 0)

    def _set_root_password(self):
        password = str(self.runtime_config.get("security_root_password") or "")
        if len(password) < 12 or "\n" in password or "\r" in password:
            raise CommandError(
                "Configure una nueva contraseña root de al menos 12 caracteres."
            )
        run(
            ["chpasswd"],
            timeout=30,
            input_data=f"root:{password}\n",
        )

    @staticmethod
    def _ssh_socket_active():
        return bool(
            run(
                ["systemctl", "is-active", "--quiet", "ssh.socket"],
                check=False,
                timeout=20,
            ).get("success")
        )

    def _write_ssh_profile(self, ports, password_auth=None, permit_root=None):
        lines = [
            "# Managed by Genesis Admin Agent.",
            *[f"Port {port}" for port in sorted(set(ports))],
        ]
        if password_auth is not None:
            lines.append(
                f"PasswordAuthentication {'yes' if password_auth else 'no'}"
            )
            if not password_auth:
                lines.append("KbdInteractiveAuthentication no")
        if permit_root is not None:
            if not permit_root:
                value = "no"
            elif password_auth:
                value = "yes"
            else:
                value = "prohibit-password"
            lines.append(f"PermitRootLogin {value}")
        content = "\n".join(lines) + "\n"
        previous = SSH_PROFILE.read_text(encoding="utf-8") if SSH_PROFILE.exists() else None
        self._atomic_write(SSH_PROFILE, content)
        validation = run(["sshd", "-t"], check=False, timeout=30)
        if not validation.get("success"):
            if previous is None:
                SSH_PROFILE.unlink(missing_ok=True)
            else:
                self._atomic_write(SSH_PROFILE, previous)
            raise CommandError(
                "La configuración SSH generada no superó sshd -t: "
                f"{validation.get('output') or 'error desconocido'}"
            )

    def _write_socket_profile(self, ports):
        if not self._ssh_socket_active():
            return False
        previous = (
            SSH_SOCKET_PROFILE.read_text(encoding="utf-8")
            if SSH_SOCKET_PROFILE.exists()
            else None
        )
        lines = [
            "[Socket]",
            "ListenStream=",
            *[f"ListenStream={port}" for port in sorted(set(ports))],
        ]
        self._atomic_write(SSH_SOCKET_PROFILE, "\n".join(lines) + "\n")
        try:
            run(["systemctl", "daemon-reload"], timeout=60)
            run(["systemctl", "restart", "ssh.socket"], timeout=120)
        except Exception:
            if previous is None:
                SSH_SOCKET_PROFILE.unlink(missing_ok=True)
            else:
                self._atomic_write(SSH_SOCKET_PROFILE, previous)
            run(["systemctl", "daemon-reload"], check=False, timeout=60)
            run(
                ["systemctl", "restart", "ssh.socket"],
                check=False,
                timeout=120,
            )
            raise
        return True

    @staticmethod
    def _port_is_listening(port):
        result = run(["ss", "-H", "-ltn"], check=False, timeout=30)
        for line in str(result.get("output") or "").splitlines():
            columns = line.split()
            if len(columns) > 3:
                local_address = columns[3]
                if local_address.rsplit(":", 1)[-1] == str(port):
                    return True
        return False

    @staticmethod
    def _reload_ssh(socket_active):
        if socket_active:
            return
        result = run(
            ["systemctl", "reload", "ssh.service"],
            check=False,
            timeout=120,
        )
        if not result.get("success"):
            run(["systemctl", "reload", "sshd.service"], timeout=120)

    @staticmethod
    def _configure_ufw(ports, enable=False, remove_ports=()):
        run(["apt-get", "install", "-y", "ufw"], timeout=600)
        for port in sorted(set(ports)):
            run(["ufw", "allow", f"{port}/tcp"], timeout=60)
        for port in (80, 443):
            run(["ufw", "allow", f"{port}/tcp"], timeout=60)
        if enable:
            run(["ufw", "--force", "enable"], timeout=120)
            for port in sorted(set(remove_ports) - set(ports)):
                run(
                    ["ufw", "--force", "delete", "allow", f"{port}/tcp"],
                    check=False,
                    timeout=60,
                )

    def prepare_security(self, payload):
        self._require_root()
        payload = dict(payload or {})
        target_port = self._validate_port(payload.get("ssh_port"))
        keys = self._parse_authorized_keys(payload.get("authorized_keys"))
        previous_ports = self._current_ssh_ports()
        retained_ports = sorted(set(previous_ports + [target_port]))
        self._progress("keys", 20, "Instalando claves públicas SSH para root.")
        self._install_root_keys(keys)
        self._progress("password", 35, "Actualizando la contraseña root.")
        self._set_root_password()
        if payload.get("ufw_enabled"):
            self._progress("firewall", 50, "Preparando reglas UFW sin cerrar el acceso actual.")
            self._configure_ufw(retained_ports, enable=False)
        self._progress("ssh", 65, "Habilitando el puerto nuevo y conservando los actuales.")
        self._write_ssh_profile(retained_ports)
        socket_active = self._write_socket_profile(retained_ports)
        self._reload_ssh(socket_active)
        if not self._port_is_listening(target_port):
            raise CommandError(
                f"SSH no quedó escuchando en el puerto nuevo {target_port}."
            )
        self._progress("verify", 90, "Validando la configuración SSH preparada.")
        run(["sshd", "-t"], timeout=30)
        self._progress("finished", 100, "Seguridad preparada; falta confirmar el acceso.")
        return {
            "success": True,
            "security_state": "prepared",
            "target_ssh_port": target_port,
            "previous_ssh_ports": previous_ports,
            "retained_ssh_ports": retained_ports,
            "requires_confirmation": True,
            "password_changed": True,
            "authorized_key_count": len(keys),
            "message": (
                "Configuración preparada. Pruebe el acceso por el puerto nuevo "
                "antes de aplicar el cierre definitivo."
            ),
        }

    def enforce_security(self, payload):
        self._require_root()
        payload = dict(payload or {})
        target_port = self._validate_port(payload.get("ssh_port"))
        previous_ports = [
            self._validate_port(port)
            for port in (payload.get("previous_ssh_ports") or [])
        ]
        if not Path("/root/.ssh/authorized_keys").is_file():
            raise CommandError(
                "No se aplicó la seguridad porque root no tiene authorized_keys."
            )
        self._progress("ssh", 30, "Aplicando la política SSH definitiva.")
        self._write_ssh_profile(
            [target_port],
            password_auth=bool(payload.get("password_auth")),
            permit_root=bool(payload.get("permit_root")),
        )
        socket_active = self._write_socket_profile([target_port])
        self._reload_ssh(socket_active)
        if not self._port_is_listening(target_port):
            raise CommandError(
                f"SSH no quedó escuchando en el puerto definitivo {target_port}."
            )
        self._progress("firewall", 65, "Aplicando la política de firewall.")
        if payload.get("ufw_enabled"):
            self._configure_ufw(
                [target_port],
                enable=True,
                remove_ports=previous_ports,
            )
        self._progress("verify", 90, "Validando la política SSH definitiva.")
        run(["sshd", "-t"], timeout=30)
        self._progress("finished", 100, "Perfil de seguridad aplicado.")
        return {
            "success": True,
            "security_state": "applied",
            "target_ssh_port": target_port,
            "password_authentication": bool(payload.get("password_auth")),
            "permit_root": bool(payload.get("permit_root")),
            "ufw_enabled": bool(payload.get("ufw_enabled")),
            "message": "Perfil de seguridad aplicado y validado.",
        }
