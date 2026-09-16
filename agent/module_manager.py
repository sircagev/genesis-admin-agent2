import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .commands import CommandError, run, systemd_status
from .discovery import OdooServiceDiscovery
from .provisioner import OdooProvisioner


MODULE_RE = re.compile(r"^[A-Za-z0-9_]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATABASE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
RESULT_PREFIX = "GENESIS_MODULE_RESULT="
ACTIONS = {"install", "upgrade", "uninstall"}
PHASES = {"before_code", "after_code"}


class OdooModuleManager:
    """Inventory and execute typed module operations for discovered services."""

    def __init__(
        self,
        config,
        progress_callback=None,
        runner=None,
        discovery=None,
    ):
        self.config = config
        self.progress_callback = progress_callback
        self.runner = runner or run
        self.discovery = discovery or OdooServiceDiscovery(config)
        self.provisioner = OdooProvisioner(config)
        self.tool_path = Path(__file__).with_name("odoo_module_tool.py")

    def set_runtime_config(self, values):
        self.provisioner.set_runtime_config(values)

    def clear_runtime_config(self):
        self.provisioner.clear_runtime_config()

    def _progress(self, stage, percent, message):
        if callable(self.progress_callback):
            self.progress_callback(stage, percent, message)

    @staticmethod
    def _validate_module_name(value):
        name = str(value or "").strip()
        if not MODULE_RE.fullmatch(name):
            raise CommandError(f"Nombre de modulo invalido: {name!r}")
        return name

    @staticmethod
    def _validate_database(value):
        database = str(value or "").strip()
        if not DATABASE_RE.fullmatch(database):
            raise CommandError("Nombre de base de datos invalido.")
        return database

    @staticmethod
    def _repository_identity(value):
        value = str(value or "").strip()
        credential = False
        if value.startswith("git@"):
            match = re.fullmatch(r"git@([^:]+):(.+)", value)
            host = match.group(1) if match else ""
            path = match.group(2) if match else ""
        else:
            parsed = urlparse(value)
            host = parsed.hostname or ""
            path = parsed.path.lstrip("/")
            credential = bool(parsed.username or parsed.password)
        if path.endswith(".git"):
            path = path[:-4]
        return f"{host.lower()}/{path.strip('/').lower()}", credential

    def _validate_repository(self, values):
        values = values if isinstance(values, dict) else {}
        url = str(values.get("url") or "").strip()
        branch = str(values.get("branch") or "").strip()
        subpaths = values.get("subpaths") or ["custom_addons", "modulos"]
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
        ):
            raise CommandError(
                "El repositorio debe ser una URL HTTPS de GitHub sin credenciales."
            )
        identity, _credential = self._repository_identity(url)
        if identity.count("/") != 2:
            raise CommandError("Repositorio GitHub invalido.")
        if (
            not BRANCH_RE.fullmatch(branch)
            or ".." in branch
            or branch.startswith("/")
            or branch.endswith("/")
        ):
            raise CommandError("Rama de modulos invalida.")
        if not isinstance(subpaths, list) or not subpaths:
            raise CommandError("Las subrutas de addons no son validas.")
        normalized = []
        for value in subpaths:
            path = str(value or "").strip().strip("/")
            if (
                not path
                or path.startswith(".")
                or ".." in path.split("/")
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            ):
                raise CommandError("Subruta de addons invalida.")
            if path not in normalized:
                normalized.append(path)
        return {"url": url, "branch": branch, "subpaths": normalized}

    def _validate_steps(self, values):
        if not isinstance(values, list):
            raise CommandError("Los pasos de modulos deben ser una lista.")
        steps = []
        for position, raw in enumerate(values, start=1):
            if not isinstance(raw, dict):
                raise CommandError("Paso de modulos invalido.")
            action = str(raw.get("action") or "").strip()
            if action not in ACTIONS:
                raise CommandError("Accion de modulos no soportada.")
            module = self._validate_module_name(raw.get("module"))
            phase = str(raw.get("phase") or "").strip()
            if not phase:
                phase = "before_code" if action == "uninstall" else "after_code"
            if phase not in PHASES:
                raise CommandError("Fase de modulos no soportada.")
            if action in ("install", "upgrade") and phase == "before_code":
                raise CommandError(
                    "Instalaciones y actualizaciones deben ejecutarse despues del codigo."
                )
            sequence = int(raw.get("sequence") or position * 10)
            if sequence < 1 or sequence > 99999:
                raise CommandError("Secuencia de modulo invalida.")
            steps.append(
                {
                    "sequence": sequence,
                    "action": action,
                    "module": module,
                    "phase": phase,
                }
            )
        steps.sort(key=lambda item: (item["sequence"], item["module"]))
        return steps

    def _context(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        unit = str(payload.get("service_name") or "").strip()
        if not unit:
            raise CommandError("Falta la unidad Odoo.")
        database = self._validate_database(payload.get("database_name"))
        repository = self._validate_repository(payload.get("repository"))

        discovered = self.discovery.discover()
        services = discovered.get("services") or []
        service = next(
            (item for item in services if item.get("unit") == unit),
            None,
        )
        if not service:
            raise CommandError("El servicio Odoo no fue descubierto.")
        if not service.get("control_allowed"):
            raise CommandError("El servicio Odoo no esta permitido para control.")

        inventory = self.discovery.discover_databases(services)
        database_item = next(
            (
                item
                for item in (inventory.get("databases") or [])
                if item.get("name") == database
                and item.get("service_unit") == unit
                and item.get("association_state") == "matched"
            ),
            None,
        )
        if not database_item:
            raise CommandError(
                "La base no esta asociada de forma inequivoca al servicio."
            )

        runtime = self._runtime(service)
        repo = self._locate_repository(service, runtime, repository)
        return {
            "service": service,
            "database": database,
            "database_item": database_item,
            "repository": repository,
            "repo": repo,
            "runtime": runtime,
        }

    @staticmethod
    def _runtime(service):
        exec_start = str(service.get("exec_start") or "")
        python_path = ""
        odoo_bin = ""

        match = re.search(r"(?:^|\s)path=([^ ;]+)", exec_start)
        if match:
            python_path = match.group(1)
        argv_match = re.search(r"argv\[\]=(.+?)\s;\s", exec_start)
        argv_text = argv_match.group(1) if argv_match else exec_start
        try:
            arguments = shlex.split(argv_text)
        except ValueError:
            arguments = argv_text.split()
        for value in arguments:
            if value.endswith("/odoo-bin"):
                odoo_bin = value
                break
        if not python_path and arguments:
            python_path = arguments[0]

        # Do not resolve the virtualenv python symlink: resolving it would
        # silently replace the instance interpreter with /usr/bin/python.
        python = Path(python_path)
        executable = Path(odoo_bin).resolve()
        config = Path(str(service.get("config_path") or "")).resolve()
        if not python.is_file() or not executable.is_file() or not config.is_file():
            raise CommandError("No se pudieron validar ejecutable y configuracion Odoo.")
        root = executable.parent
        if root.parent.parent != Path("/opt") and Path("/opt") not in root.parents:
            raise CommandError("La instalacion Odoo esta fuera de /opt.")
        return {
            "python": python,
            "odoo_bin": executable,
            "odoo_root": root,
            "config": config,
            "version": str(service.get("odoo_version") or ""),
        }

    def _git_local(self, repo, arguments, check=True, timeout=120):
        command = [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            *arguments,
        ]
        result = self.runner(command, check=False, timeout=timeout)
        if check and not result.get("success"):
            raise CommandError("No fue posible inspeccionar el repositorio local.")
        return result

    def _locate_repository(self, service, runtime, repository):
        expected, _credential = self._repository_identity(repository["url"])
        candidates = []
        for raw in str(service.get("addons_path") or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw).resolve()
            for candidate in (path, *path.parents):
                if runtime["odoo_root"] not in candidate.parents and candidate != runtime["odoo_root"]:
                    continue
                if (candidate / ".git").is_dir():
                    candidates.append(candidate)
                    break
                if candidate == runtime["odoo_root"]:
                    break

        matches = []
        for candidate in sorted(set(candidates)):
            result = self._git_local(
                candidate,
                ["remote", "get-url", "origin"],
                check=False,
            )
            if not result.get("success"):
                continue
            identity, credential = self._repository_identity(result.get("output"))
            if identity == expected:
                matches.append((candidate, credential))
        if len(matches) != 1:
            raise CommandError(
                "No se encontro un unico checkout del repositorio en addons_path."
            )
        return {"path": matches[0][0], "credential_in_origin": matches[0][1]}

    def _repo_state(self, context):
        repo = context["repo"]["path"]
        head = str(
            self._git_local(repo, ["rev-parse", "HEAD"]).get("output") or ""
        ).strip().lower()
        branch = str(
            self._git_local(
                repo,
                ["branch", "--show-current"],
            ).get("output")
            or ""
        ).strip()
        dirty = bool(
            str(
                self._git_local(
                    repo,
                    ["status", "--porcelain=v1", "--untracked-files=all"],
                ).get("output")
                or ""
            ).strip()
        )
        if not SHA_RE.fullmatch(head):
            raise CommandError("Git devolvio una revision local invalida.")
        return {
            "head": head,
            "branch": branch,
            "dirty": dirty,
            "credential_in_origin": bool(
                context["repo"]["credential_in_origin"]
            ),
        }

    def _fetch_target(self, context):
        repo = context["repo"]["path"]
        repository = context["repository"]
        branch = repository["branch"]
        remote_ref = f"refs/remotes/genesis-managed/{branch}"
        self.provisioner._run_git(
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "-C",
                str(repo),
                "fetch",
                "--quiet",
                repository["url"],
                f"refs/heads/{branch}:{remote_ref}",
            ],
            repository["url"],
            timeout=600,
        )
        target = str(
            self._git_local(repo, ["rev-parse", remote_ref]).get("output")
            or ""
        ).strip().lower()
        if not SHA_RE.fullmatch(target):
            raise CommandError("Git devolvio una revision remota invalida.")
        return target

    @staticmethod
    def _manifest(path):
        try:
            value = ast.literal_eval(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _odoo_series(value):
        raw = str(value or "").strip()
        match = re.search(
            r"(?<![0-9])([0-9]+)(?:[.]([0-9]+))?",
            raw,
        )
        if not match:
            raise CommandError(
                f"Version principal de Odoo invalida: {raw!r}"
            )
        return f"{int(match.group(1))}.{int(match.group(2) or 0)}"

    @classmethod
    def _adapt_manifest_version(cls, value, odoo_version):
        """Apply the same canonical version rule used by Odoo manifests."""
        series = cls._odoo_series(odoo_version)
        version = "1.0" if value is None else str(value).strip()
        parts = version.split(".")
        if not 2 <= len(parts) <= 5:
            raise CommandError(
                f"Version de modulo invalida: {version!r}; debe tener "
                "entre 2 y 5 componentes."
            )
        try:
            [int(part) for part in parts]
        except ValueError as exc:
            raise CommandError(
                f"Version de modulo invalida: {version!r}"
            ) from exc
        if len(parts) <= 3 and not version.startswith(series):
            version = f"{series}.{version}"
        if not version.startswith(f"{series}."):
            raise CommandError(
                f"Version de modulo {version!r} incompatible con Odoo "
                f"{series}."
            )
        return version

    @classmethod
    def _catalog_manifest_version(cls, manifest, odoo_version):
        raw_version = (
            manifest["version"]
            if "version" in manifest
            else None
        )
        try:
            return (
                cls._adapt_manifest_version(
                    raw_version,
                    odoo_version,
                ),
                "",
            )
        except CommandError as exc:
            return str(raw_version or "").strip(), str(exc)

    def _scan_catalog(self, root, subpaths, odoo_version):
        modules = {}
        for subpath in subpaths:
            addons_root = (Path(root) / subpath).resolve()
            if Path(root).resolve() not in addons_root.parents:
                raise CommandError("Subruta de addons fuera del repositorio.")
            if not addons_root.is_dir():
                continue
            for module_dir in sorted(addons_root.iterdir()):
                if not module_dir.is_dir() or not MODULE_RE.fullmatch(module_dir.name):
                    continue
                manifest_path = module_dir / "__manifest__.py"
                if not manifest_path.is_file():
                    manifest_path = module_dir / "__openerp__.py"
                if not manifest_path.is_file():
                    continue
                manifest = self._manifest(manifest_path)
                if manifest is None:
                    continue
                version, version_error = self._catalog_manifest_version(
                    manifest,
                    odoo_version,
                )
                dependencies = manifest.get("depends") or []
                if not isinstance(dependencies, (list, tuple)):
                    dependencies = []
                modules[module_dir.name] = {
                    "name": module_dir.name,
                    "summary": str(
                        manifest.get("summary")
                        or manifest.get("name")
                        or module_dir.name
                    ),
                    "version": version,
                    "version_error": version_error,
                    "installable": bool(
                        manifest.get("installable", True)
                    ) and not version_error,
                    "application": bool(manifest.get("application", False)),
                    "auto_install": bool(manifest.get("auto_install", False)),
                    "dependencies": sorted(
                        {
                            str(item)
                            for item in dependencies
                            if MODULE_RE.fullmatch(str(item))
                        }
                    ),
                    "external_dependencies": manifest.get(
                        "external_dependencies"
                    )
                    if isinstance(
                        manifest.get("external_dependencies"),
                        dict,
                    )
                    else {},
                    "required_models": sorted(
                        {
                            str(item).strip()
                            for item in (
                                manifest.get("genesis_required_models")
                                or []
                            )
                            if str(item).strip()
                        }
                    )
                    if isinstance(
                        manifest.get("genesis_required_models"),
                        (list, tuple),
                    )
                    else [],
                    "subpath": subpath,
                }
        return modules

    def _target_catalog(self, context, target_sha):
        repo = context["repo"]["path"]
        with tempfile.TemporaryDirectory(
            prefix="genesis-modules-plan-"
        ) as temporary:
            checkout = Path(temporary) / "release"
            added = False
            try:
                self._git_local(
                    repo,
                    [
                        "worktree",
                        "add",
                        "--detach",
                        "--quiet",
                        str(checkout),
                        target_sha,
                    ],
                    timeout=300,
                )
                added = True
                return self._scan_catalog(
                    checkout,
                    context["repository"]["subpaths"],
                    context["runtime"]["version"],
                )
            finally:
                if added:
                    self._git_local(
                        repo,
                        ["worktree", "remove", "--force", str(checkout)],
                        timeout=120,
                    )

    def _run_tool(self, context, mode, modules=None):
        request = {
            "mode": mode,
            "modules": list(modules or []),
        }
        descriptor, name = tempfile.mkstemp(
            prefix="genesis-module-request-",
            suffix=".json",
        )
        request_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(request, handle)
            os.chmod(request_path, 0o644)
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(context["runtime"]["python"]),
                str(self.tool_path),
                "--odoo-root",
                str(context["runtime"]["odoo_root"]),
                "--config",
                str(context["runtime"]["config"]),
                "--database",
                context["database"],
                "--request",
                str(request_path),
            ]
            result = self.runner(command, check=False, timeout=1800)
            output = str(result.get("output") or "")
            if not result.get("success"):
                detail = output.strip()[-2000:]
                raise CommandError(
                    "Fallo la operacion interna de modulos."
                    + (f" Detalle: {detail}" if detail else "")
                )
            line = next(
                (
                    value[len(RESULT_PREFIX):]
                    for value in reversed(output.splitlines())
                    if value.startswith(RESULT_PREFIX)
                ),
                "",
            )
            if not line:
                raise CommandError("La utilidad Odoo no devolvio resultado.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CommandError("Resultado de modulos invalido.")
            return value
        finally:
            request_path.unlink(missing_ok=True)

    @staticmethod
    def _dependency_closure(module, catalog):
        pending = [module]
        result = set()
        while pending:
            current = pending.pop()
            values = catalog.get(current) or {}
            for dependency in values.get("dependencies") or []:
                if dependency not in result:
                    result.add(dependency)
                    if dependency in catalog:
                        pending.append(dependency)
        result.discard(module)
        return sorted(result)

    def _dependency_preflight(self, context, catalog, steps):
        roots = sorted(
            {
                step["module"]
                for step in steps
                if step.get("action") in ("install", "upgrade")
                and step.get("module")
            }
        )
        if not roots:
            return {
                "requested_modules": [],
                "dependencies_found": [],
                "missing_dependencies": [],
                "install_order": [],
                "states_before": [],
                "warnings": [],
                "blockers": [],
            }
        runtime = self._run_tool(context, "preflight", roots)
        runtime_nodes = {
            item.get("name"): item
            for item in runtime.get("modules") or []
            if isinstance(item, dict) and item.get("name")
        }
        addons_paths = {
            str(Path(value.strip()).resolve())
            for value in str(
                context["service"].get("addons_path") or ""
            ).split(",")
            if value.strip()
        }
        blockers, missing, found, order, states = [], [], [], [], []
        warnings = []
        visiting, visited = [], set()
        for item in runtime.get("pending_modules") or []:
            blockers.append(
                "Hay una operacion de modulo pendiente: "
                f"{item.get('name')} ({item.get('state')})."
            )

        def fail(chain, reason):
            missing.append({"chain": chain, "reason": reason})
            blockers.append(" -> ".join(chain) + f" ({reason})")

        def visit(name, chain):
            if name in visiting:
                fail([*chain, name], "ciclo de dependencias")
                return
            if name in visited:
                return
            target = catalog.get(name)
            node = runtime_nodes.get(name) or {}
            expected_path = None
            state = str(node.get("database_state") or "not_registered")
            reused_dependency = name not in roots and state == "installed"
            if target:
                expected_path = (
                    Path(context["repo"]["path"])
                    / str(target.get("subpath") or "")
                    / name
                ).resolve()
                if str(expected_path.parent) not in addons_paths:
                    if not reused_dependency:
                        fail(
                            [*chain, name],
                            "existe en Git pero su directorio no esta en addons_path",
                        )
                        return
                    warnings.append(
                        f"{name}: se reutiliza la dependencia instalada "
                        "desde su addons_path efectivo."
                    )
                if node.get("recognized") and str(
                    Path(node.get("path") or "").resolve()
                ) != str(expected_path):
                    if not reused_dependency:
                        fail(
                            [*chain, name],
                            "Odoo resuelve otro modulo con el mismo nombre",
                        )
                        return
                    warnings.append(
                        f"{name}: Odoo resuelve una dependencia instalada "
                        "desde otra ruta."
                    )
                dependencies = target.get("dependencies") or []
            else:
                if not node.get("recognized"):
                    fail(
                        [*chain, name],
                        "faltante en addons_path o no reconocido por Odoo",
                    )
                    return
                dependencies = node.get("dependencies") or []
            states.append({"name": name, "state": state})
            if state in {"to install", "to upgrade", "to remove"}:
                fail([*chain, name], f"estado inconsistente: {state}")
                return
            if state == "uninstallable":
                fail([*chain, name], "modulo no instalable")
                return
            if node.get("recognized") and not node.get("can_install"):
                fail(
                    [*chain, name],
                    node.get("error") or "Odoo no puede instalar el modulo",
                )
                return
            visiting.append(name)
            for dependency in sorted(set(dependencies)):
                visit(str(dependency), [*chain, name])
            visiting.pop()
            visited.add(name)
            found.append(
                {
                    "name": name,
                    "path": str(expected_path or node.get("path") or ""),
                    "state": state,
                    "dependencies": sorted(set(dependencies)),
                    "required_models": (
                        target.get("required_models") or []
                        if target
                        else []
                    ),
                }
            )
            order.append(name)

        for root in roots:
            visit(root, [])
        for name in runtime.get("graph_omitted") or []:
            if name in visited:
                fail([name], "omitido por module_graph")
        if runtime.get("graph_error"):
            blockers.append(
                "No fue posible validar module_graph: "
                + str(runtime["graph_error"])
            )
        return {
            "requested_modules": roots,
            "dependencies_found": found,
            "missing_dependencies": missing,
            "install_order": order,
            "states_before": states,
            "warnings": list(dict.fromkeys(warnings)),
            "addons_path": sorted(addons_paths),
            "blockers": list(dict.fromkeys(blockers)),
        }

    @staticmethod
    def _fingerprint(data):
        encoded = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _combined_inventory(catalog, installed):
        modules = []
        for name in sorted(set(catalog) | set(installed)):
            values = catalog.get(name) or {}
            current = installed.get(name) or {}
            managed = bool(values)
            modules.append(
                {
                    "name": name,
                    "summary": str(
                        values.get("summary")
                        or current.get("summary")
                        or name
                    ),
                    "version": str(
                        values.get("version")
                        or current.get("available_version")
                        or current.get("latest_version")
                        or ""
                    ),
                    "installable": bool(
                        values.get("installable", False)
                    ),
                    "application": bool(
                        values.get(
                            "application",
                            current.get("application", False),
                        )
                    ),
                    "auto_install": bool(values.get("auto_install", False)),
                    "dependencies": values.get("dependencies") or [],
                    "external_dependencies": (
                        values.get("external_dependencies") or {}
                    ),
                    "subpath": values.get("subpath") or "",
                    "managed": managed,
                    "state": current.get("state") or "uninstalled",
                    "installed_version": current.get("installed_version") or "",
                    "database_dependencies": current.get("dependencies") or [],
                }
            )
        return modules

    def inventory(self, payload):
        self._progress("discover", 15, "Validando servicio y base de datos.")
        context = self._context(payload)
        state = self._repo_state(context)
        self._progress("database", 55, "Leyendo modulos instalados.")
        database = self._run_tool(context, "inventory")
        self._progress("catalog", 75, "Leyendo catalogo de addons.")
        catalog = self._scan_catalog(
            context["repo"]["path"],
            context["repository"]["subpaths"],
            context["runtime"]["version"],
        )
        installed = {
            item["name"]: item
            for item in database.get("modules") or []
            if isinstance(item, dict) and item.get("name")
        }
        modules = self._combined_inventory(catalog, installed)
        self._progress("finished", 100, "Inventario de modulos terminado.")
        return {
            "success": True,
            "message": f"Se inventariaron {len(modules)} modulos custom.",
            "database_name": context["database"],
            "service_name": context["service"]["unit"],
            "odoo_version": context["runtime"]["version"],
            "repository_path": str(context["repo"]["path"]),
            "repository_state": state,
            "modules": modules,
            "count": len(modules),
        }

    @staticmethod
    def _version_key(value):
        version = str(value or "").strip()
        if not re.fullmatch(r"[0-9]+(?:[.][0-9]+)*", version):
            raise CommandError(
                f"Version de modulo invalida: {version!r}"
            )
        parts = [int(part) for part in version.split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    @classmethod
    def _version_compare(cls, left, right):
        left_key = cls._version_key(left)
        right_key = cls._version_key(right)
        width = max(len(left_key), len(right_key))
        left_key += (0,) * (width - len(left_key))
        right_key += (0,) * (width - len(right_key))
        return (left_key > right_key) - (left_key < right_key)

    @classmethod
    def _effective_version(cls, installed_version, applied_version):
        installed = str(installed_version or "").strip()
        applied = str(applied_version or "").strip()
        if not installed:
            return applied
        if not applied:
            return installed
        try:
            return (
                applied
                if cls._version_compare(applied, installed) > 0
                else installed
            )
        except CommandError:
            return installed

    def _validate_migration_rules(self, values):
        if not isinstance(values, list):
            raise CommandError(
                "Las reglas de migracion deben ser una lista."
            )
        rules = []
        seen = set()
        for raw in values:
            if not isinstance(raw, dict):
                raise CommandError("Regla de migracion invalida.")
            module = self._validate_module_name(raw.get("module"))
            target_version = str(
                raw.get("target_version") or ""
            ).strip()
            target_key = self._version_key(target_version)
            identity = (module, target_key)
            if identity in seen:
                raise CommandError(
                    "Hay reglas de migracion duplicadas para "
                    f"{module} {target_version}."
                )
            seen.add(identity)
            raw_steps = raw.get("steps") or []
            if not isinstance(raw_steps, list):
                raise CommandError(
                    "Los pasos de una migracion deben ser una lista."
                )
            steps = []
            for position, raw_step in enumerate(raw_steps, start=1):
                if not isinstance(raw_step, dict):
                    raise CommandError("Paso de migracion invalido.")
                action = str(raw_step.get("action") or "").strip()
                if action not in {
                    "restart",
                    "verify",
                    "install",
                    "upgrade",
                    "uninstall",
                }:
                    raise CommandError(
                        "Accion de migracion no soportada."
                    )
                module_name = str(
                    raw_step.get("module") or ""
                ).strip()
                if action != "restart":
                    module_name = self._validate_module_name(
                        module_name
                    )
                elif module_name:
                    raise CommandError(
                        "Reiniciar servicio no recibe un modulo."
                    )
                required_version = str(
                    raw_step.get("required_version") or ""
                ).strip()
                if required_version:
                    self._version_key(required_version)
                phase = str(
                    raw_step.get("phase") or "after_code"
                ).strip()
                if phase not in PHASES:
                    raise CommandError(
                        "Fase de migracion no soportada."
                    )
                if action == "restart" and phase != "after_code":
                    raise CommandError(
                        "El reinicio intermedio ocurre despues del codigo."
                    )
                if (
                    action in ("install", "upgrade")
                    and phase != "after_code"
                ):
                    raise CommandError(
                        "Dependencias instaladas o actualizadas "
                        "deben ejecutarse despues del codigo."
                    )
                steps.append(
                    {
                        "sequence": int(
                            raw_step.get("sequence")
                            or position * 10
                        ),
                        "action": action,
                        "module": module_name,
                        "required_version": required_version,
                        "phase": phase,
                        "notes": str(
                            raw_step.get("notes") or ""
                        ).strip(),
                    }
                )
            steps.sort(
                key=lambda item: (
                    item["sequence"],
                    item["module"],
                    item["action"],
                )
            )
            rules.append(
                {
                    "module": module,
                    "target_version": target_version,
                    "target_key": target_key,
                    "apply_on_install": bool(
                        raw.get("apply_on_install")
                    ),
                    "skip_module_operation": bool(
                        raw.get("skip_module_operation")
                    ),
                    "steps": steps,
                }
            )
        rules.sort(
            key=lambda item: (
                item["module"],
                item["target_key"],
            )
        )
        return rules

    def _expand_versioned_steps(
        self,
        manual_steps,
        selection_mode,
        catalog,
        installed,
        migration_rules,
        applied_versions,
    ):
        blockers = []
        applied_versions = (
            applied_versions
            if isinstance(applied_versions, dict)
            else {}
        )
        automatic = []
        for name, values in sorted(catalog.items()):
            current = installed.get(name) or {}
            if current.get("state") not in (
                "installed",
                "to upgrade",
            ):
                continue
            if values.get("version_error"):
                blockers.append(
                    f"{name}: {values['version_error']}"
                )
                continue
            installed_version = str(
                current.get("installed_version") or ""
            ).strip()
            effective = self._effective_version(
                installed_version,
                applied_versions.get(name),
            )
            target = str(values.get("version") or "").strip()
            if not effective:
                automatic.append(
                    {
                        "sequence": len(automatic) * 10 + 10,
                        "action": "upgrade",
                        "module": name,
                        "phase": "after_code",
                        "reason": (
                            "La version instalada no fue informada; "
                            "se actualiza para sincronizarla"
                        ),
                    }
                )
                continue
            try:
                comparison = self._version_compare(
                    target,
                    effective,
                )
            except CommandError as exc:
                blockers.append(f"{name}: {exc}")
                continue
            if comparison > 0:
                automatic.append(
                    {
                        "sequence": len(automatic) * 10 + 10,
                        "action": "upgrade",
                        "module": name,
                        "phase": "after_code",
                        "reason": (
                            "Version instalada diferente "
                            "al commit objetivo"
                        ),
                    }
                )
            elif comparison < 0:
                blockers.append(
                    f"{name}: la version disponible {target} "
                    f"es anterior a la aplicada {effective}."
                )

        if selection_mode == "update_installed":
            requested = automatic
            if not requested:
                blockers.append(
                    "No se requieren actualizaciones: todos los modulos "
                    "administrados instalados ya tienen la version disponible."
                )
        else:
            manual_modules = {
                step["module"]
                for step in manual_steps
            }
            remaining_automatic = [
                step
                for step in automatic
                if step["module"] not in manual_modules
            ]
            if any(
                step["action"] == "install"
                for step in manual_steps
            ):
                requested = [
                    *remaining_automatic,
                    *manual_steps,
                ]
            else:
                requested = [
                    *manual_steps,
                    *remaining_automatic,
                ]

        rules_by_module = {}
        for rule in migration_rules:
            rules_by_module.setdefault(rule["module"], []).append(rule)

        expanded = []
        proposed_versions = {}
        stack = []

        def append_step(step, transition_module="", transition_version=""):
            expanded.append(
                {
                    **step,
                    "sequence": (len(expanded) + 1) * 10,
                    "transition_module": transition_module,
                    "transition_version": transition_version,
                }
            )

        def expand_action(step, transition_module="", transition_version=""):
            action = step["action"]
            name = step.get("module") or ""
            if action not in ("install", "upgrade"):
                append_step(
                    step,
                    transition_module,
                    transition_version,
                )
                if action == "uninstall" and name:
                    proposed_versions[name] = ""
                return

            values = catalog.get(name)
            current = installed.get(name) or {
                "state": "uninstalled",
            }
            if not values:
                blockers.append(
                    f"El modulo {name} no existe en el commit objetivo."
                )
                return
            if values.get("version_error"):
                blockers.append(
                    f"{name}: {values['version_error']}"
                )
                return
            if not values.get("installable"):
                blockers.append(f"El modulo {name} no es instalable.")
                return
            if (
                action == "upgrade"
                and current.get("state")
                not in ("installed", "to upgrade")
            ):
                # An upgrade never installs a missing module implicitly.
                # Keep the deployment executable and omit this operation.
                return

            target_version = str(
                values.get("version") or ""
            ).strip()
            try:
                target_key = self._version_key(target_version)
            except CommandError as exc:
                blockers.append(f"{name}: {exc}")
                return

            installed_version = str(
                current.get("installed_version") or ""
            ).strip()
            effective = self._effective_version(
                installed_version,
                applied_versions.get(name),
            )
            current_key = ()
            source_version_known = bool(effective)
            if action == "upgrade":
                if source_version_known:
                    try:
                        current_key = self._version_key(effective)
                    except CommandError as exc:
                        blockers.append(f"{name}: {exc}")
                        return
                    if self._version_compare(
                        target_version,
                        effective,
                    ) < 0:
                        blockers.append(
                            f"{name}: no se permite bajar de "
                            f"{effective} a {target_version}."
                        )
                        return

            required = str(
                step.get("required_version") or ""
            ).strip()
            if required:
                try:
                    if self._version_compare(
                        target_version,
                        required,
                    ) < 0:
                        blockers.append(
                            f"{name}: el repo ofrece {target_version}, "
                            f"pero se requiere al menos {required}."
                        )
                        return
                except CommandError as exc:
                    blockers.append(f"{name}: {exc}")
                    return

            if name in stack:
                blockers.append(
                    "Ciclo en reglas de migracion: "
                    + " -> ".join([*stack, name])
                )
                return
            stack.append(name)
            applicable = []
            for rule in rules_by_module.get(name, []):
                if rule["target_key"] > target_key:
                    continue
                if action == "install":
                    if rule["apply_on_install"]:
                        applicable.append(rule)
                elif (
                    source_version_known
                    and rule["target_key"] > current_key
                ):
                    applicable.append(rule)

            for rule in applicable:
                for migration_step in rule["steps"]:
                    nested = {
                        **migration_step,
                        "reason": (
                            f"{name} -> "
                            f"{rule['target_version']}"
                        ),
                    }
                    if (
                        nested["action"] in ("install", "upgrade")
                        and nested["module"] != name
                    ):
                        expand_action(
                            nested,
                            transition_module=name,
                            transition_version=(
                                rule["target_version"]
                            ),
                        )
                    else:
                        append_step(
                            nested,
                            transition_module=name,
                            transition_version=(
                                rule["target_version"]
                            ),
                        )

            skip_operation = bool(
                action == "upgrade"
                and applicable
                and applicable[-1]["target_key"] == target_key
                and applicable[-1]["skip_module_operation"]
            )
            if not skip_operation:
                append_step(
                    {
                        **step,
                        "required_version": target_version,
                        "reason": (
                            step.get("reason")
                            or "Operacion solicitada"
                        ),
                    },
                    transition_module,
                    transition_version,
                )
            proposed_versions[name] = target_version
            stack.pop()

        for step in requested:
            expand_action(step)

        deduplicated = []
        seen_operations = set()
        for step in expanded:
            if step["action"] in ("install", "upgrade"):
                identity = (
                    step["action"],
                    step["module"],
                    step.get("required_version") or "",
                )
                if identity in seen_operations:
                    continue
                seen_operations.add(identity)
            deduplicated.append(step)

        for position, step in enumerate(deduplicated, start=1):
            step["sequence"] = position * 10

        return (
            deduplicated,
            blockers,
            proposed_versions,
            requested,
        )

    def _enrich_planned_steps(
        self,
        context,
        steps,
        catalog,
        installed,
        applied_versions,
    ):
        blockers = []
        planned = []
        projected_states = {
            name: values.get("state") or "uninstalled"
            for name, values in installed.items()
        }
        projected_versions = {
            name: self._effective_version(
                values.get("installed_version"),
                applied_versions.get(name),
            )
            for name, values in installed.items()
        }
        for step in steps:
            action = step["action"]
            name = step.get("module") or ""
            if action == "restart":
                planned.append(
                    {
                        **step,
                        "current_state": "",
                        "installed_version": "",
                        "target_version": "",
                        "dependencies": [],
                        "impacted_modules": [],
                    }
                )
                continue

            current = installed.get(name) or {
                "state": "uninstalled",
            }
            values = catalog.get(name)
            state = projected_states.get(
                name,
                current.get("state") or "uninstalled",
            )
            effective = projected_versions.get(
                name,
                self._effective_version(
                    current.get("installed_version"),
                    applied_versions.get(name),
                ),
            )
            required = str(
                step.get("required_version") or ""
            ).strip()
            dependencies = []
            impact = []

            if action == "verify":
                if state not in ("installed", "to upgrade"):
                    blockers.append(
                        f"{name}: la verificacion requiere el modulo instalado."
                    )
                elif required:
                    try:
                        if self._version_compare(
                            effective,
                            required,
                        ) < 0:
                            blockers.append(
                                f"{name}: se verificara al menos "
                                f"{required}, pero el plan deja {effective}."
                            )
                    except CommandError as exc:
                        blockers.append(f"{name}: {exc}")
            elif action == "uninstall":
                impact_result = self._run_tool(
                    context,
                    "impact",
                    [name],
                )
                impact = [
                    item.get("name")
                    for item in impact_result.get("impacted") or []
                    if item.get("name")
                ]
                if state not in ("installed", "to upgrade"):
                    impact = []
                projected_states[name] = "uninstalled"
                projected_versions[name] = ""
            else:
                if not values:
                    blockers.append(
                        f"El modulo {name} no existe en el commit objetivo."
                    )
                else:
                    dependencies = self._dependency_closure(
                        name,
                        catalog,
                    )
                    target = str(
                        values.get("version") or ""
                    ).strip()
                    if required:
                        try:
                            if self._version_compare(
                                target,
                                required,
                            ) < 0:
                                blockers.append(
                                    f"{name}: el repo ofrece {target}, "
                                    f"pero se requiere {required}."
                                )
                        except CommandError as exc:
                            blockers.append(f"{name}: {exc}")
                    projected_states[name] = "installed"
                    projected_versions[name] = target

            planned.append(
                {
                    **step,
                    "current_state": (
                        current.get("state") or "uninstalled"
                    ),
                    "installed_version": (
                        self._effective_version(
                            current.get("installed_version"),
                            applied_versions.get(name),
                        )
                    ),
                    "target_version": (
                        (values or {}).get("version") or required
                    ),
                    "dependencies": dependencies,
                    "impacted_modules": sorted(set(impact)),
                }
            )
        return planned, blockers

    def plan(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        selection_mode = str(
            payload.get("selection_mode") or "manual"
        ).strip()
        if selection_mode not in ("manual", "update_installed"):
            raise CommandError("Alcance de modulos no soportado.")
        manual_steps = self._validate_steps(
            payload.get("steps") or []
        )
        if selection_mode == "manual" and not manual_steps:
            raise CommandError(
                "El despliegue manual requiere al menos un paso."
            )
        migration_rules = self._validate_migration_rules(
            payload.get("migration_rules") or []
        )
        applied_versions = payload.get("applied_versions") or {}
        if not isinstance(applied_versions, dict):
            raise CommandError(
                "Las versiones aplicadas deben ser un objeto."
            )
        for module, version in applied_versions.items():
            self._validate_module_name(module)
            self._version_key(version)

        self._progress("discover", 10, "Validando servicio, base y repositorio.")
        context = self._context(payload)
        state = self._repo_state(context)

        self._progress("fetch", 25, "Descargando metadatos del codigo objetivo.")
        target_sha = self._fetch_target(context)
        expected_sha = str(payload.get("target_sha") or "").strip().lower()
        if expected_sha and expected_sha != target_sha:
            raise CommandError(
                "La rama avanzo desde el analisis; vuelva a simular el lote."
            )

        fast_forward = bool(
            self._git_local(
                context["repo"]["path"],
                ["merge-base", "--is-ancestor", state["head"], target_sha],
                check=False,
            ).get("success")
        )
        self._progress("catalog", 45, "Analizando manifests del commit objetivo.")
        catalog = self._target_catalog(context, target_sha)
        database = self._run_tool(context, "inventory")
        installed = {
            item["name"]: item
            for item in database.get("modules") or []
            if isinstance(item, dict) and item.get("name")
        }

        expanded, blockers, proposed_versions, requested = (
            self._expand_versioned_steps(
                manual_steps,
                selection_mode,
                catalog,
                installed,
                migration_rules,
                applied_versions,
            )
        )
        dependency_preflight = self._dependency_preflight(
            context,
            catalog,
            expanded,
        )
        blockers.extend(dependency_preflight["blockers"])
        planned, validation_blockers = self._enrich_planned_steps(
            context,
            expanded,
            catalog,
            installed,
            applied_versions,
        )
        blockers.extend(validation_blockers)

        if state["branch"] != context["repository"]["branch"]:
            blockers.append(
                "La rama local no coincide con la rama administrada."
            )
        if state["dirty"]:
            blockers.append("El repositorio tiene cambios locales.")
        if state["credential_in_origin"]:
            blockers.append("El origin contiene credenciales.")
        if not fast_forward:
            blockers.append(
                "El commit objetivo no es un avance fast-forward."
            )
        blockers = list(dict.fromkeys(blockers))

        catalog_modules = self._combined_inventory(catalog, installed)
        snapshot = {
            "database_name": context["database"],
            "service_name": context["service"]["unit"],
            "target_sha": target_sha,
            "selection_mode": selection_mode,
            "steps": planned,
            "applied_versions": proposed_versions,
            "dependency_preflight": dependency_preflight,
        }
        fingerprint = self._fingerprint(snapshot)
        self._progress("finished", 100, "Simulacion de modulos terminada.")
        return {
            "success": True,
            "simulation": True,
            "message": (
                "Simulacion correcta y lista para aprobar."
                if not blockers
                else "Simulacion terminada con bloqueos de ejecucion."
            ),
            "database_name": context["database"],
            "service_name": context["service"]["unit"],
            "odoo_version": context["runtime"]["version"],
            "repository_path": str(context["repo"]["path"]),
            "repository_state": state,
            "target_sha": target_sha,
            "target_short_sha": target_sha[:12],
            "fast_forward": fast_forward,
            "selection_mode": selection_mode,
            "requested_count": len(requested),
            "steps": planned,
            "blockers": blockers,
            "executable": not blockers,
            "plan_fingerprint": fingerprint,
            "catalog_count": len(catalog),
            "modules": catalog_modules,
            "applied_versions": proposed_versions,
            "dependency_preflight": dependency_preflight,
        }

    def _backup_database(self, database, target_sha):
        base = Path("/var/lib/genesis-admin-agent/module-backups")
        directory = base / database
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(base, 0o700)
        os.chmod(directory, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"{stamp}-{target_sha[:12]}.dump"
        command = [
            "runuser",
            "-u",
            "postgres",
            "--",
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            database,
        ]
        with target.open("wb") as handle:
            process = subprocess.run(
                command,
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=3600,
                check=False,
            )
        if process.returncode != 0:
            target.unlink(missing_ok=True)
            raise CommandError("No fue posible crear el backup previo.")
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        os.chmod(target, 0o600)
        return {
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": digest.hexdigest(),
        }

    def _module_state(self, context, name):
        inventory = self._run_tool(context, "inventory")
        values = next(
            (
                item
                for item in inventory.get("modules") or []
                if item.get("name") == name
            ),
            None,
        )
        return values or {"name": name, "state": "uninstalled"}

    def _execute_step(self, context, step):
        action = step["action"]
        if action == "restart":
            raise CommandError(
                "El reinicio debe ejecutarse como frontera de servicio."
            )

        name = step["module"]
        current = self._module_state(context, name)
        installed = current.get("state") in ("installed", "to upgrade")
        installed_version = str(
            current.get("installed_version") or ""
        ).strip()
        required = str(
            step.get("required_version") or ""
        ).strip()

        if action == "verify":
            if not installed:
                raise CommandError(
                    f"El modulo {name} no esta instalado."
                )
            if required:
                if not installed_version:
                    raise CommandError(
                        f"El modulo {name} no informa su version; "
                        "no es posible verificar el requisito."
                    )
                if self._version_compare(
                    installed_version,
                    required,
                ) < 0:
                    raise CommandError(
                        f"El modulo {name} tiene {installed_version} "
                        f"y requiere al menos {required}."
                    )
            return {
                "module": name,
                "action": action,
                "status": "success",
                "installed_version": installed_version,
            }

        execution_action = action
        if action == "install" and installed:
            if required:
                if not installed_version or self._version_compare(
                    installed_version,
                    required,
                ) < 0:
                    execution_action = "upgrade"
                else:
                    return {
                        "module": name,
                        "action": action,
                        "status": "skipped",
                        "installed_version": installed_version,
                    }
            else:
                return {
                    "module": name,
                    "action": action,
                    "status": "skipped",
                    "installed_version": installed_version,
                }
        if action == "uninstall" and not installed:
            return {
                "module": name,
                "action": action,
                "status": "skipped",
            }
        if action == "upgrade" and not installed:
            return {
                "module": name,
                "action": action,
                "status": "skipped",
                "reason": "not_installed",
            }
        if (
            action == "upgrade"
            and required
            and installed_version
            and self._version_compare(
                installed_version,
                required,
            ) >= 0
        ):
            return {
                "module": name,
                "action": action,
                "status": "skipped",
                "installed_version": installed_version,
            }

        if action == "uninstall":
            result = self._run_tool(context, "uninstall", [name])
            return {
                "module": name,
                "action": action,
                "status": "success",
                "impact": [
                    item.get("name")
                    for item in result.get("impacted") or []
                    if item.get("name")
                ],
            }

        runtime = context["runtime"]
        if runtime["version"] == "19":
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(runtime["python"]),
                str(runtime["odoo_bin"]),
                "module",
                execution_action,
                "-c",
                str(runtime["config"]),
                "-d",
                context["database"],
                name,
            ]
        else:
            option = "-i" if execution_action == "install" else "-u"
            command = [
                "runuser",
                "-u",
                "odoo",
                "--",
                str(runtime["python"]),
                str(runtime["odoo_bin"]),
                "-c",
                str(runtime["config"]),
                "-d",
                context["database"],
                option,
                name,
                "--stop-after-init",
                "--no-http",
            ]
        result = self.runner(command, check=False, timeout=3600)
        if not result.get("success"):
            output = str(result.get("output") or "").strip()[-2500:]
            raise CommandError(
                f"Fallo {execution_action} de {name}."
                + (f" Detalle: {output}" if output else "")
            )

        verified = self._module_state(context, name)
        verified_version = str(
            verified.get("installed_version") or ""
        ).strip()
        verified_state = str(verified.get("state") or "uninstalled")
        if verified_state != "installed":
            raise CommandError(
                f"{name} termino en estado {verified_state}, "
                "no en installed."
            )
        result = {
            "module": name,
            "action": action,
            "executed_action": execution_action,
            "status": "success",
            "installed_version": verified_version,
            "state_after": verified_state,
        }
        if required:
            if not verified_version:
                result["version_warning"] = (
                    "Odoo no informo la version despues de la operacion; "
                    "se registrara la version administrada del plan."
                )
            elif self._version_compare(
                verified_version,
                required,
            ) < 0:
                raise CommandError(
                    f"{name} termino en {verified_version}, "
                    f"pero se requiere al menos {required}."
                )
        return result

    def _restart_service_boundary(self, context):
        unit = context["service"]["unit"]
        started = self.runner(
            ["systemctl", "start", unit],
            check=False,
            timeout=180,
        )
        if not started.get("success"):
            raise CommandError(
                "No fue posible iniciar el servicio "
                "durante el reinicio intermedio."
            )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if systemd_status(unit).get("active_state") == "active":
                    break
                time.sleep(2)
            else:
                raise CommandError(
                    "El servicio no quedo activo durante "
                    "el reinicio intermedio."
                )
        finally:
            stopped = self.runner(
                ["systemctl", "stop", unit],
                check=False,
                timeout=120,
            )
            if not stopped.get("success"):
                raise CommandError(
                    "No fue posible volver a detener el servicio "
                    "despues del reinicio intermedio."
                )
        return {
            "module": "",
            "action": "restart",
            "status": "success",
        }

    def _merge_code(self, context, target_sha):
        repo = context["repo"]["path"]
        self._git_local(
            repo,
            ["merge", "--ff-only", target_sha],
            timeout=600,
        )
        requirements = repo / "requirements.txt"
        if requirements.is_file():
            result = self.runner(
                [
                    str(context["runtime"]["python"].parent / "pip"),
                    "install",
                    "--upgrade",
                    "-r",
                    str(requirements),
                ],
                check=False,
                timeout=1800,
            )
            if not result.get("success"):
                raise CommandError(
                    "No fue posible instalar requirements del repositorio."
                )

    def apply(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        approved = str(payload.get("plan_fingerprint") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", approved):
            raise CommandError("Falta la huella del plan aprobado.")

        self._progress("revalidate", 5, "Revalidando el plan aprobado.")
        planned = self.plan(payload)
        if planned["plan_fingerprint"] != approved:
            raise CommandError(
                "El impacto cambio desde la simulacion; vuelva a aprobar el lote."
            )
        if not planned.get("executable"):
            raise CommandError(
                "El lote tiene bloqueos: " + "; ".join(planned["blockers"])
            )

        context = self._context(payload)
        steps = planned["steps"]
        target_sha = planned["target_sha"]
        if (
            any(step["action"] == "uninstall" for step in steps)
            and not payload.get("backup_required", True)
        ):
            raise CommandError(
                "Toda desinstalacion requiere backup previo."
            )
        backup = None
        if payload.get("backup_required", True):
            self._progress("backup", 20, "Creando backup previo de la base.")
            backup = self._backup_database(context["database"], target_sha)

        status = systemd_status(context["service"]["unit"])
        was_running = status.get("active_state") == "active"
        results = []
        failure = None
        final_inventory = None
        postflight = {}
        current_step = {}
        try:
            if was_running:
                self._progress("stop", 32, "Deteniendo servicio Odoo.")
                stopped = self.runner(
                    ["systemctl", "stop", context["service"]["unit"]],
                    check=False,
                    timeout=120,
                )
                if not stopped.get("success"):
                    raise CommandError("No fue posible detener el servicio Odoo.")

            before = [step for step in steps if step["phase"] == "before_code"]
            after = [step for step in steps if step["phase"] == "after_code"]
            for index, step in enumerate(before, start=1):
                current_step = step
                self._progress(
                    "before_code",
                    35 + min(index * 4, 12),
                    f"Ejecutando {step['action']} de {step['module']}.",
                )
                results.append(self._execute_step(context, step))

            self._progress("code", 52, "Aplicando commit de modulos aprobado.")
            self._merge_code(context, target_sha)
            runtime_catalog = self._scan_catalog(
                context["repo"]["path"],
                context["repository"]["subpaths"],
                context["runtime"]["version"],
            )
            runtime_preflight = self._dependency_preflight(
                context,
                runtime_catalog,
                steps,
            )
            if runtime_preflight["blockers"]:
                raise CommandError(
                    "Preflight posterior al merge bloqueado: "
                    + "; ".join(runtime_preflight["blockers"])
                )

            for index, step in enumerate(after, start=1):
                current_step = step
                self._progress(
                    "modules",
                    58 + min(index * 6, 28),
                    f"Ejecutando {step['action']} de {step['module']}.",
                )
                if step["action"] == "restart":
                    results.append(
                        self._restart_service_boundary(context)
                    )
                else:
                    results.append(
                        self._execute_step(context, step)
                    )

            self._progress("verify", 90, "Verificando estados de modulos.")
            postflight = self._run_tool(
                context,
                "preflight",
                planned["dependency_preflight"]["requested_modules"],
            )
            postflight_nodes = {
                item.get("name"): item
                for item in postflight.get("modules") or []
                if isinstance(item, dict) and item.get("name")
            }
            expected_modules = planned["dependency_preflight"]["install_order"]
            invalid_after = [
                name
                for name in expected_modules
                if postflight_nodes.get(name, {}).get("database_state")
                != "installed"
            ]
            missing_models = [
                f"{item['name']}.{model_name}"
                for item in planned["dependency_preflight"][
                    "dependencies_found"
                ]
                for model_name in item.get("required_models") or []
                if model_name
                not in set(
                    postflight_nodes.get(item["name"], {}).get(
                        "registered_models"
                    ) or []
                )
            ]
            if (
                invalid_after
                or missing_models
                or postflight.get("pending_modules")
                or postflight.get("graph_omitted")
            ):
                raise CommandError(
                    "Verificacion posterior incompleta: "
                    + ", ".join(
                        invalid_after
                        or missing_models
                        or postflight.get("graph_omitted")
                        or []
                    )
                )
            final_inventory = self.inventory(
                {
                    **payload,
                    "steps": [],
                }
            )
        except Exception as exc:  # pylint: disable=broad-except
            failure = exc
            try:
                postflight = self._run_tool(
                    context,
                    "preflight",
                    planned["dependency_preflight"]["requested_modules"],
                )
                final_inventory = self.inventory(
                    {
                        **payload,
                        "steps": [],
                    }
                )
            except Exception:  # Preserve the original deployment failure.
                pass
        finally:
            if was_running:
                self._progress("start", 95, "Iniciando servicio Odoo.")
                started = self.runner(
                    ["systemctl", "start", context["service"]["unit"]],
                    check=False,
                    timeout=180,
                )
                if not started.get("success") and failure is None:
                    failure = CommandError(
                        "Los modulos terminaron, pero el servicio no inicio."
                    )

        if failure is not None:
            recovery = ""
            if backup:
                recovery = (
                    f" Backup previo: {backup['path']} "
                    f"(sha256 {backup['sha256']})."
                )
            applied = [
                item["module"]
                for item in results
                if item.get("status") == "success"
                and item.get("module")
            ]
            partial = (
                " Pasos aplicados: " + ", ".join(applied) + "."
                if applied
                else ""
            )
            return {
                "success": False,
                "message": f"{failure}{recovery}{partial}",
                "error": str(failure),
                "database_name": context["database"],
                "service_name": context["service"]["unit"],
                "target_sha": target_sha,
                "plan_fingerprint": approved,
                "backup": backup or {},
                "recovery_required": bool(backup),
                "steps": results,
                "inventory": final_inventory or {},
                "dependency_preflight": planned.get(
                    "dependency_preflight"
                ) or {},
                "postflight": postflight or {},
                "requested_modules": planned.get(
                    "dependency_preflight", {}
                ).get("requested_modules") or [],
                "root_module": (
                    current_step.get("transition_module")
                    or current_step.get("module")
                    or ""
                ),
                "states_before": planned.get(
                    "dependency_preflight", {}
                ).get("states_before") or [],
                "states_after": [
                    {
                        "name": item.get("name"),
                        "state": item.get("database_state"),
                    }
                    for item in postflight.get("modules") or []
                    if isinstance(item, dict)
                ],
                "applied_versions": {},
            }

        health = systemd_status(context["service"]["unit"])
        self._progress("finished", 100, "Despliegue de modulos terminado.")
        return {
            "success": True,
            "message": "Despliegue de modulos terminado correctamente.",
            "database_name": context["database"],
            "service_name": context["service"]["unit"],
            "target_sha": target_sha,
            "plan_fingerprint": approved,
            "backup": backup or {},
            "steps": results,
            "inventory": final_inventory or {},
            "dependency_preflight": planned.get(
                "dependency_preflight"
            ) or {},
            "postflight": postflight or {},
            "requested_modules": planned.get(
                "dependency_preflight", {}
            ).get("requested_modules") or [],
            "states_before": planned.get(
                "dependency_preflight", {}
            ).get("states_before") or [],
            "states_after": [
                {
                    "name": item.get("name"),
                    "state": item.get("database_state"),
                }
                for item in postflight.get("modules") or []
                if isinstance(item, dict)
            ],
            "applied_versions": (
                planned.get("applied_versions") or {}
            ),
            "status": health,
        }
