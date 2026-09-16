import argparse
import json
import sys
from pathlib import Path


RESULT_PREFIX = "GENESIS_MODULE_RESULT="


def _parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--odoo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--request", required=True)
    return parser


def _load_request(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError("La solicitud de modulos no es valida.")
    return value


def _environment(args):
    root = Path(args.odoo_root).resolve()
    if not (root / "odoo-bin").is_file():
        raise RuntimeError("No se encontro odoo-bin.")
    sys.path.insert(0, str(root))

    from odoo.api import Environment
    try:
        from odoo.api import SUPERUSER_ID
    except ImportError:
        from odoo import SUPERUSER_ID
    from odoo.modules.registry import Registry
    from odoo.tools import config

    config.parse_config(
        [
            "--no-http",
            "-c",
            str(Path(args.config).resolve()),
            "-d",
            args.database,
        ]
    )
    return Environment, Registry, SUPERUSER_ID


def _module_values(module):
    # Odoo keeps historically inverted field names:
    # latest_version is installed in the database, while
    # installed_version is the latest version available on disk.
    database_version = getattr(module, "latest_version", "") or ""
    disk_version = getattr(module, "installed_version", "") or ""
    values = {
        "name": module.name,
        "state": module.state,
        "installed_version": database_version,
        "available_version": disk_version,
        "latest_version": disk_version,
        "summary": getattr(module, "summary", "") or "",
        "application": bool(getattr(module, "application", False)),
        "dependencies": [],
    }
    dependencies = getattr(module, "dependencies_id", False)
    if dependencies:
        values["dependencies"] = sorted(
            {
                dependency.name
                for dependency in dependencies
                if dependency.name
            }
        )
    return values


def _inventory(env):
    Module = env["ir.module.module"]
    modules = Module.search(
        [
            (
                "state",
                "in",
                (
                    "installed",
                    "to install",
                    "to upgrade",
                    "to remove",
                ),
            )
        ],
        order="name",
    )
    return {
        "modules": [_module_values(module) for module in modules],
        "count": len(modules),
    }


def _preflight(env, names):
    """Inspect module availability without scheduling a module operation."""
    from odoo.modules.module import (
        Manifest,
        get_module_path,
        initialize_sys_path,
    )
    from odoo.modules.module_graph import ModuleGraph
    from odoo.tools import config

    initialize_sys_path()
    Module = env["ir.module.module"]
    records = {
        module.name: module
        for module in Module.search([], order="name")
    }
    pending_states = {"to install", "to upgrade", "to remove"}
    pending_modules = [
        _module_values(module)
        for module in records.values()
        if module.state in pending_states
    ]
    nodes = {}
    missing = []
    queue = [(name, []) for name in sorted(set(names))]
    while queue:
        name, chain = queue.pop(0)
        if name in nodes:
            continue
        path = get_module_path(name, display_warning=False)
        manifest = Manifest.for_addon(name, display_warning=False)
        record = records.get(name)
        if not path or manifest is None:
            nodes[name] = {
                "name": name,
                "path": "",
                "recognized": False,
                "installable": False,
                "database_state": record.state if record else "not_registered",
                "dependencies": [],
                "can_install": False,
                "error": "Modulo no encontrado en addons_path.",
            }
            missing.append({"module": name, "chain": [*chain, name]})
            continue
        dependencies = sorted(
            {
                str(item)
                for item in (manifest.get("depends") or [])
                if str(item)
            }
        )
        external_error = ""
        try:
            Module.check_external_dependencies(name, "to install")
        except Exception as exc:  # Odoo returns a user-facing diagnostic.
            external_error = str(exc)
        installable = bool(manifest.get("installable", True))
        state = record.state if record else "not_registered"
        registered_models = sorted(
            model_name
            for model_name in env.registry
            if getattr(env.registry[model_name], "_module", "") == name
        )
        nodes[name] = {
            "name": name,
            "path": str(path),
            "recognized": True,
            "installable": installable,
            "database_state": state,
            "dependencies": dependencies,
            "registered_models": registered_models,
            "can_install": bool(
                installable
                and state != "uninstallable"
                and not external_error
            ),
            "error": external_error,
        }
        for dependency in dependencies:
            queue.append((dependency, [*chain, name]))

    graph_omitted = []
    graph_error = ""
    graph_candidates = [
        name for name, node in nodes.items() if node["recognized"]
    ]
    if graph_candidates:
        try:
            graph = ModuleGraph(env.cr, mode="update")
            graph.extend(graph_candidates)
            graph_names = {node.name for node in graph}
            graph_omitted = sorted(set(graph_candidates) - graph_names)
        except Exception as exc:  # Keep preflight diagnostic, never hide it.
            graph_error = str(exc)

    return {
        "modules": [nodes[name] for name in sorted(nodes)],
        "missing_dependencies": missing,
        "pending_modules": pending_modules,
        "graph_omitted": graph_omitted,
        "graph_error": graph_error,
        "addons_path": [str(path) for path in config["addons_path"]],
    }


def _impact(env, names):
    Module = env["ir.module.module"]
    selected = Module.search([("name", "in", names)])
    found = set(selected.mapped("name"))
    missing = sorted(set(names) - found)
    installed = selected.filtered(
        lambda module: module.state in ("installed", "to upgrade")
    )
    downstream = Module.browse()
    if installed:
        try:
            downstream = installed.downstream_dependencies(installed)
        except TypeError:
            downstream = installed.downstream_dependencies()
    impacted = (installed | downstream).sorted("name")
    return {
        "selected": [_module_values(module) for module in selected.sorted("name")],
        "impacted": [_module_values(module) for module in impacted],
        "missing": missing,
    }


def _uninstall(env, names):
    Module = env["ir.module.module"]
    selected = Module.search([("name", "in", names)])
    found = set(selected.mapped("name"))
    missing = sorted(set(names) - found)
    if missing:
        raise RuntimeError(
            "No se encontraron modulos para desinstalar: "
            + ", ".join(missing)
        )
    invalid = selected.filtered(
        lambda module: module.state not in ("installed", "to upgrade")
    )
    if invalid:
        raise RuntimeError(
            "Los modulos no estan instalados: "
            + ", ".join(invalid.mapped("name"))
        )
    before = _impact(env, names)
    selected.button_immediate_uninstall()
    return {
        "uninstalled": sorted(found),
        "impacted": before["impacted"],
    }


def main():
    args = _parser().parse_args()
    request = _load_request(args.request)
    mode = str(request.get("mode") or "")
    names = request.get("modules") or []
    if not isinstance(names, list) or not all(
        isinstance(name, str) for name in names
    ):
        raise RuntimeError("La lista de modulos no es valida.")

    Environment, Registry, superuser_id = _environment(args)
    with Registry.new(args.database).cursor() as cursor:
        env = Environment(cursor, superuser_id, {})
        if mode == "inventory":
            result = _inventory(env)
        elif mode == "preflight":
            result = _preflight(env, names)
        elif mode == "impact":
            result = _impact(env, names)
        elif mode == "uninstall":
            result = _uninstall(env, names)
        else:
            raise RuntimeError("Operacion de modulos no soportada.")

    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
