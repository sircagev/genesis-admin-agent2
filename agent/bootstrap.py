import grp
import pwd
import shutil
from pathlib import Path

from .commands import run, systemd_status


EXECUTABLE_COMPONENTS = {
    "git": ("git",),
    "http_client": ("curl", "wget"),
    "python3": ("python3",),
    "pip": ("pip3", "pip"),
    "build_tools": ("gcc", "make"),
    "nodejs": ("node",),
    "rtlcss": ("rtlcss",),
    "wkhtmltopdf": ("wkhtmltopdf",),
    "wkhtmltoimage": ("wkhtmltoimage",),
    "postgresql": ("psql", "pg_isready"),
    "nginx": ("nginx",),
    "certbot": ("certbot",),
}

DEVELOPMENT_PACKAGE_REQUIREMENTS = (
    ("python3-dev",),
    ("python3-venv",),
    ("python3-pip",),
    ("python3-wheel",),
    ("build-essential",),
    ("libzip-dev",),
    ("libxslt1-dev",),
    ("libldap2-dev",),
    ("libsasl2-dev",),
    ("libjpeg-dev",),
    ("libpng-dev",),
    ("libcairo2-dev",),
    # Ubuntu/Debian cambió el nombre del paquete de desarrollo. Ambos
    # proporcionan las cabeceras Freetype requeridas por Odoo.
    ("libfreetype6-dev", "libfreetype-dev"),
    ("libpq-dev",),
    ("libffi-dev",),
    ("fontconfig",),
    ("python3-certbot-nginx",),
)

DEVELOPMENT_PACKAGES = tuple(
    package
    for alternatives in DEVELOPMENT_PACKAGE_REQUIREMENTS
    for package in alternatives
)

REQUIRED_COMPONENTS = tuple(EXECUTABLE_COMPONENTS) + (
    "development_libraries",
    "certbot_nginx",
    "odoo_user",
)


def _command_paths(names):
    return [shutil.which(name) for name in names]


def collect_bootstrap_inventory():
    components = {}
    for key, names in EXECUTABLE_COMPONENTS.items():
        paths = _command_paths(names)
        installed = all(paths) if key not in ("http_client",) else any(paths)
        components[key] = {
            "status": "installed" if installed else "missing",
            "executables": [path for path in paths if path],
        }

    certbot_nginx = any(
        path.exists()
        for path in (
            Path("/usr/lib/python3/dist-packages/certbot_nginx"),
            Path("/usr/local/lib/python3/dist-packages/certbot_nginx"),
        )
    )
    components["certbot_nginx"] = {
        "status": "installed" if certbot_nginx else "missing",
    }

    try:
        account = pwd.getpwnam("odoo")
        group = grp.getgrgid(account.pw_gid)
        home = account.pw_dir
        user_status = (
            "installed"
            if home in ("/opt/odoo", "/opt") and group.gr_name == "odoo"
            else "wrong_version"
        )
        user_details = {
            "home": home,
            "uid": account.pw_uid,
            "group": group.gr_name,
        }
    except KeyError:
        user_status = "missing"
        user_details = {}
    components["odoo_user"] = {
        "status": user_status,
        **user_details,
    }

    components["development_libraries"] = {
        "status": "missing",
        "message": "Requiere auditoría detallada mediante bootstrap.audit.",
        "packages": list(DEVELOPMENT_PACKAGES),
    }

    ready = all(
        item.get("status") == "installed"
        for item in components.values()
    )
    return {
        "ready": ready,
        "components": components,
        "required_components": list(REQUIRED_COMPONENTS),
        "apt_upgrade_allowed": False,
    }


class ServerBootstrapAuditor:
    """Read-only prerequisite audit used by the future idempotent installer."""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

    @staticmethod
    def _version(executable):
        if not executable:
            return ""
        result = run([executable, "--version"], check=False, timeout=20)
        return str(result.get("output") or "").splitlines()[0][:240]

    def audit(self, _payload=None):
        self._progress("dependencies", 15, "Validando dependencias del sistema.")
        inventory = collect_bootstrap_inventory()
        components = inventory["components"]
        self._progress("python", 25, "Validando Python y ejecutables.")
        for key, names in EXECUTABLE_COMPONENTS.items():
            paths = _command_paths(names)
            executable = next((path for path in paths if path), None)
            if executable:
                try:
                    components[key]["version"] = self._version(executable)
                except Exception as exc:  # pylint: disable=broad-except
                    components[key] = {"status": "error", "message": str(exc)}

        missing_packages = []
        package_errors = []
        installed_packages = []
        for alternatives in DEVELOPMENT_PACKAGE_REQUIREMENTS:
            requirement_installed = False
            for package in alternatives:
                try:
                    package_result = run(
                        ["dpkg-query", "-W", "-f=${Status}", package],
                        check=False,
                        timeout=20,
                    )
                    installed = bool(
                        package_result.get("success")
                        and "install ok installed"
                        in str(package_result.get("output") or "")
                    )
                    if installed:
                        installed_packages.append(package)
                        requirement_installed = True
                except Exception as exc:  # pylint: disable=broad-except
                    package_errors.append(f"{package}: {exc}")
            if not requirement_installed:
                missing_packages.append(" | ".join(alternatives))
        components["development_libraries"] = {
            "status": (
                "error" if package_errors else "missing" if missing_packages else "installed"
            ),
            "packages": list(DEVELOPMENT_PACKAGES),
            "installed": installed_packages,
            "missing": missing_packages,
            "errors": package_errors,
        }

        self._progress("nodejs", 40, "Validando Node.js y rtlcss.")
        node = components.get("nodejs", {})
        node_version = node.get("version", "")
        if node.get("status") == "installed" and node_version:
            major = node_version.lower().lstrip("vnode ").split(".", 1)[0]
            if major.isdigit() and int(major) != 20:
                node["status"] = "wrong_version"
                node["expected"] = "20.x"

        self._progress("wkhtmltopdf", 48, "Validando wkhtmltopdf.")
        wkhtml = components.get("wkhtmltopdf", {})
        if wkhtml.get("status") == "installed" and "0.12.6.1" not in wkhtml.get("version", ""):
            wkhtml["status"] = "wrong_version"
            wkhtml["expected"] = "0.12.6.1"

        self._progress("postgresql", 60, "Validando PostgreSQL y conexión local.")
        postgres = components.get("postgresql", {})
        if postgres.get("status") == "installed":
            status = systemd_status("postgresql.service")
            readiness = run(["pg_isready", "-h", "127.0.0.1"], check=False, timeout=20)
            postgres["service"] = status
            postgres["localhost_ready"] = bool(readiness.get("success"))
            settings = run(
                [
                    "runuser", "-u", "postgres", "--", "psql",
                    "-At", "-d", "postgres", "-c",
                    "SHOW password_encryption; SHOW hba_file;",
                ],
                check=False,
                timeout=20,
            )
            setting_lines = str(settings.get("output") or "").splitlines()
            postgres["settings_query_success"] = bool(settings.get("success"))
            postgres["password_encryption"] = setting_lines[0] if setting_lines else ""
            hba_file = Path(setting_lines[1]) if len(setting_lines) > 1 else None
            try:
                hba_text = hba_file.read_text(encoding="utf-8") if hba_file else ""
                postgres["tcp_scram_rule"] = any(
                    line.strip().startswith("host") and "scram-sha-256" in line
                    for line in hba_text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
            except OSError as exc:
                postgres["tcp_scram_rule"] = False
                postgres["hba_error"] = str(exc)
            if (
                status.get("active_state") != "active"
                or not postgres["localhost_ready"]
                or not postgres["settings_query_success"]
            ):
                postgres["status"] = "error"
            elif (
                postgres["password_encryption"] != "scram-sha-256"
                or not postgres["tcp_scram_rule"]
            ):
                postgres["status"] = "wrong_version"

        self._progress("nginx", 72, "Validando Nginx.")
        nginx = components.get("nginx", {})
        if nginx.get("status") == "installed":
            nginx_status = systemd_status("nginx.service")
            nginx["service"] = nginx_status
            if nginx_status.get("active_state") != "active":
                nginx["status"] = "error"

        self._progress("certbot", 80, "Validando Certbot y su plugin Nginx.")
        self._progress("odoo_user", 88, "Validando el usuario Linux odoo.")
        components["security_profile"] = {
            "status": "installed",
            "applied": False,
            "message": "Perfil opcional no aplicado durante bootstrap.",
        }
        self._progress("inventory", 94, "Consolidando inventario de bootstrap.")
        ready = all(
            components.get(key, {}).get("status") == "installed"
            for key in REQUIRED_COMPONENTS
        )
        pending = [
            key for key in REQUIRED_COMPONENTS
            if components.get(key, {}).get("status") != "installed"
        ]
        self._progress("finished", 100, "Auditoría de bootstrap terminada.")
        return {
            "success": True,
            "ready": ready,
            "components": components,
            "pending_components": pending,
            "installation_plan": pending,
            "apt_upgrade_allowed": False,
            "message": (
                "Servidor listo para Genesis Infra."
                if ready
                else "El servidor requiere componentes antes de quedar listo."
            ),
        }
