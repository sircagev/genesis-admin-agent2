import ast
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .commands import CommandError, run


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class AgentUpdater:
    """Typed, non-destructive updater for the Agent's own repository."""

    def __init__(
        self,
        config,
        progress_callback=None,
        base_path=None,
        runner=None,
        current_version=None,
    ):
        self.config = config
        self.progress_callback = progress_callback
        self.base_path = Path(base_path or "/opt/genesis-admin-agent")
        self.runner = runner or run
        self.current_version = str(current_version or __version__)
        update_config = config.section("update")
        self.remote = "origin"
        self.branch = str(update_config.get("branch") or "main").strip()
        self.expected_repository = str(
            update_config.get("repository")
            or "sircagev/genesis-admin-agent2"
        ).strip()

        if (
            not BRANCH_PATTERN.fullmatch(self.branch)
            or ".." in self.branch
            or self.branch.startswith("/")
        ):
            raise CommandError("Rama de actualización inválida.")
        if not REPOSITORY_PATTERN.fullmatch(self.expected_repository):
            raise CommandError("Repositorio esperado de actualización inválido.")

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

    def _run(self, command, message, timeout=120):
        result = self.runner(
            command,
            check=False,
            timeout=timeout,
        )
        if not result.get("success"):
            raise CommandError(message)
        return str(result.get("output") or "").strip()

    @staticmethod
    def _repository_identity(remote_url):
        value = str(remote_url or "").strip()
        if not value:
            return ""

        if value.startswith("git@"):
            match = re.fullmatch(r"git@([^:]+):(.+)", value)
            host = match.group(1) if match else ""
            path = match.group(2) if match else ""
        else:
            parsed = urlparse(value)
            if parsed.username or parsed.password:
                return ""
            host = parsed.hostname or ""
            path = parsed.path.lstrip("/")

        if host.lower() != "github.com":
            return ""
        if path.endswith(".git"):
            path = path[:-4]
        return path.strip("/")

    @staticmethod
    def _version_tuple(version):
        version = str(version or "").strip()
        if not VERSION_PATTERN.fullmatch(version):
            raise CommandError("Versión de Agent inválida.")
        return tuple(int(item) for item in version.split("."))

    def _target_version(self, target_sha):
        source = self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "show",
                f"{target_sha}:agent/__init__.py",
            ],
            "No fue posible leer la versión publicada del Agent.",
        )
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise CommandError(
                "El archivo de versión publicado no es Python válido."
            ) from exc

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            ):
                continue
            value = ast.literal_eval(node.value)
            version = str(value or "").strip()
            self._version_tuple(version)
            return version
        raise CommandError("La publicación no define __version__.")

    def _repository_status(self):
        if not (self.base_path / ".git").is_dir():
            raise CommandError(
                "La instalación del Agent no es un repositorio Git actualizable."
            )

        remote_url = self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "remote",
                "get-url",
                self.remote,
            ],
            "No fue posible validar el origen del Agent.",
        )
        identity = self._repository_identity(remote_url)
        if identity.lower() != self.expected_repository.lower():
            raise CommandError(
                "El repositorio origin no coincide con el repositorio "
                "permitido para actualizar el Agent."
            )

        current_branch = self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ],
            "La instalación del Agent no está sobre una rama administrada.",
        )
        if current_branch != self.branch:
            raise CommandError(
                "La rama instalada no coincide con la rama permitida "
                "para actualizar el Agent."
            )

    def _assert_clean_worktree(self):
        status = self.runner(
            [
                "git",
                "-C",
                str(self.base_path),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=False,
            timeout=60,
        )
        if not status.get("success"):
            raise CommandError("No fue posible validar el repositorio local.")
        if str(status.get("output") or "").strip():
            raise CommandError(
                "El Agent tiene cambios locales; la actualización automática "
                "no los descartará."
            )

    def check(self, _payload=None):
        self._progress("repository", 15, "Validando repositorio del Agent.")
        self._repository_status()

        self._progress("fetch", 35, "Consultando la versión publicada.")
        self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "fetch",
                "--quiet",
                self.remote,
                (
                    f"refs/heads/{self.branch}:"
                    f"refs/remotes/{self.remote}/{self.branch}"
                ),
            ],
            "No fue posible consultar la versión publicada del Agent.",
            timeout=300,
        )

        current_sha = self._run(
            ["git", "-C", str(self.base_path), "rev-parse", "HEAD"],
            "No fue posible identificar la revisión instalada.",
        ).lower()
        target_sha = self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "rev-parse",
                f"{self.remote}/{self.branch}",
            ],
            "No fue posible identificar la revisión publicada.",
        ).lower()
        if not SHA_PATTERN.fullmatch(current_sha) or not SHA_PATTERN.fullmatch(
            target_sha
        ):
            raise CommandError("Git devolvió una revisión inválida.")

        self._progress("version", 60, "Comparando versiones.")
        target_version = self._target_version(target_sha)
        current_tuple = self._version_tuple(self.current_version)
        target_tuple = self._version_tuple(target_version)

        if current_sha != target_sha:
            self._run(
                [
                    "git",
                    "-C",
                    str(self.base_path),
                    "merge-base",
                    "--is-ancestor",
                    current_sha,
                    target_sha,
                ],
                "La rama publicada no es un avance directo de la instalación.",
            )

        update_available = bool(
            current_sha != target_sha and target_tuple > current_tuple
        )
        restart_required = bool(
            current_sha == target_sha and target_tuple > current_tuple
        )
        requires_version_bump = bool(
            current_sha != target_sha and target_tuple <= current_tuple
        )
        self._progress("finished", 100, "Comprobación de actualización terminada.")
        return {
            "success": True,
            "current_version": self.current_version,
            "current_sha": current_sha,
            "available_version": target_version,
            "target_sha": target_sha,
            "update_available": update_available,
            "restart_required": restart_required,
            "requires_version_bump": requires_version_bump,
            "message": (
                f"Agent {target_version} disponible."
                if update_available
                else (
                    f"Agent {target_version} instalado; reinicio pendiente."
                    if restart_required
                    else (
                        "La rama remota cambió sin incrementar la versión."
                        if requires_version_bump
                        else f"Agent {self.current_version} está actualizado."
                    )
                )
            ),
        }

    def _install_target_requirements(self, target_sha):
        requirements = self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "show",
                f"{target_sha}:requirements.txt",
            ],
            "No fue posible leer requirements.txt de la publicación.",
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="genesis-agent-requirements-",
            suffix=".txt",
        ) as handle:
            handle.write(requirements)
            handle.flush()
            self._run(
                [
                    str(self.base_path / "venv/bin/pip"),
                    "install",
                    "--upgrade",
                    "-r",
                    handle.name,
                ],
                "No fue posible instalar las dependencias de la actualización.",
                timeout=900,
            )

    def _preflight_target(self, target_sha, target_version):
        with tempfile.TemporaryDirectory(
            prefix="genesis-agent-preflight-"
        ) as temporary:
            checkout = Path(temporary) / "release"
            added = False
            try:
                self._run(
                    [
                        "git",
                        "-C",
                        str(self.base_path),
                        "worktree",
                        "add",
                        "--detach",
                        "--quiet",
                        str(checkout),
                        target_sha,
                    ],
                    "No fue posible preparar la revisión para validación.",
                    timeout=300,
                )
                added = True
                script = (
                    "import sys; "
                    f"sys.path.insert(0, {str(checkout)!r}); "
                    "from agent import __version__; "
                    "from agent.config import AgentConfig; "
                    "from agent.executor import JobExecutor; "
                    f"assert __version__ == {target_version!r}; "
                    "JobExecutor(AgentConfig()); "
                    "print('preflight ok')"
                )
                self._run(
                    [
                        str(self.base_path / "venv/bin/python"),
                        "-c",
                        script,
                    ],
                    "La revisión publicada no superó el preflight del Agent.",
                    timeout=180,
                )
            finally:
                if added:
                    self._run(
                        [
                            "git",
                            "-C",
                            str(self.base_path),
                            "worktree",
                            "remove",
                            "--force",
                            str(checkout),
                        ],
                        "No fue posible limpiar el preflight del Agent.",
                        timeout=120,
                    )

    def _refresh_service_definition(self):
        service_source = (
            self.base_path
            / "systemd/genesis-admin-agent.service.tpl"
        )
        service_target = Path(
            "/etc/systemd/system/genesis-admin-agent.service"
        )
        service_target.write_text(
            service_source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        cli = self.base_path / "bin/genesis-agent"
        cli.chmod(0o755)
        link = Path("/usr/local/bin/genesis-agent")
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or link.resolve() != cli:
                link.unlink()
        if not link.exists() and not link.is_symlink():
            link.symlink_to(cli)
        self._run(
            ["systemctl", "daemon-reload"],
            "No fue posible recargar la configuración systemd del Agent.",
        )

    def apply(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        expected_sha = str(payload.get("target_sha") or "").strip().lower()
        expected_version = str(
            payload.get("target_version") or ""
        ).strip()
        if not SHA_PATTERN.fullmatch(expected_sha):
            raise CommandError("La actualización no contiene un SHA válido.")
        self._version_tuple(expected_version)

        self._progress("check", 10, "Revalidando publicación.")
        available = self.check({})
        if available["target_sha"] != expected_sha:
            raise CommandError(
                "La publicación cambió desde la última comprobación; "
                "vuelva a comprobar la actualización."
            )
        if available["available_version"] != expected_version:
            raise CommandError(
                "La versión publicada no coincide con la versión autorizada."
            )
        if not available["update_available"]:
            if not available.get("restart_required"):
                return {
                    **available,
                    "updated": False,
                    "restart_required": False,
                    "message": (
                        "El Agent ya estaba actualizado."
                        if available["current_sha"] == expected_sha
                        else available["message"]
                    ),
                }

            self._progress(
                "recovery",
                45,
                "Completando actualización pendiente de reinicio.",
            )
            self._assert_clean_worktree()
            self._install_target_requirements(expected_sha)
            self._preflight_target(expected_sha, expected_version)
            self._refresh_service_definition()
            return {
                **available,
                "updated": False,
                "restart_required": True,
                "message": (
                    f"Agent {expected_version} preparado; "
                    "reinicio programado después de reportar el resultado."
                ),
            }

        self._progress("working_tree", 45, "Validando instalación local.")
        self._assert_clean_worktree()

        self._progress("dependencies", 60, "Preparando dependencias.")
        self._install_target_requirements(expected_sha)

        self._progress("preflight", 70, "Validando la revisión publicada.")
        self._preflight_target(expected_sha, expected_version)

        self._progress("code", 82, "Aplicando avance fast-forward.")
        self._run(
            [
                "git",
                "-C",
                str(self.base_path),
                "merge",
                "--ff-only",
                expected_sha,
            ],
            "No fue posible aplicar la actualización fast-forward.",
            timeout=300,
        )

        self._progress("systemd", 92, "Actualizando definición del servicio.")
        self._refresh_service_definition()
        self._progress("reporting", 96, "Reportando actualización a Odoo.")
        return {
            "success": True,
            "updated": True,
            "previous_version": self.current_version,
            "available_version": expected_version,
            "target_sha": expected_sha,
            "restart_required": True,
            "message": (
                f"Agent actualizado a {expected_version}; "
                "reinicio programado después de reportar el resultado."
            ),
        }
