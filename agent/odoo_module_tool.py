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
        elif mode == "impact":
            result = _impact(env, names)
        elif mode == "uninstall":
            result = _uninstall(env, names)
        else:
            raise RuntimeError("Operacion de modulos no soportada.")

    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
