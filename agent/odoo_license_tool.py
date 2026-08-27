import argparse
import json
import secrets
import sys
from pathlib import Path


RESULT_PREFIX = "GENESIS_LICENSE_RESULT="
MODULE_NAME = "account_invoicing_limit"
PLACEHOLDER_KEYS = {"", "CAMBIAR-DESDE-LA-INTERFAZ"}


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
        raise RuntimeError("La solicitud de licencias no es valida.")
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


def _module_state(env):
    module = env["ir.module.module"].search(
        [("name", "=", MODULE_NAME)],
        limit=1,
    )
    return module.state if module else "missing"


def _bootstrap(env):
    state = _module_state(env)
    if state not in ("installed", "to upgrade"):
        raise RuntimeError(
            f"El modulo {MODULE_NAME} no esta instalado (estado: {state})."
        )
    if "license.config" not in env.registry:
        raise RuntimeError("El modelo license.config no esta disponible.")

    License = env["license.config"].sudo()
    Company = env["res.company"].sudo()
    configs = License.search([], order="id")
    existing_keys = [
        str(value).strip()
        for value in configs.mapped("api_key")
        if str(value or "").strip() not in PLACEHOLDER_KEYS
    ]
    api_key = existing_keys[0] if existing_keys else secrets.token_urlsafe(32)
    generated = not bool(existing_keys)

    existing_by_company = {item.company_id.id: item for item in configs}
    created = 0
    updated = 0
    for company in Company.search([], order="id"):
        config = existing_by_company.get(company.id)
        if not config:
            License.with_company(company).create(
                {
                    "company_id": company.id,
                    "client_name": company.name,
                    "api_key": api_key,
                }
            )
            created += 1
            continue

        values = {}
        if str(config.api_key or "").strip() in PLACEHOLDER_KEYS:
            values["api_key"] = api_key
        if not str(config.client_token or "").strip():
            values["client_token"] = secrets.token_hex(16)
        if values:
            config.write(values)
            updated += 1

    configs = License.search([], order="id")
    if not configs:
        raise RuntimeError("No fue posible crear la configuracion de licencias.")
    if not any(config.api_key == api_key for config in configs):
        raise RuntimeError("La API Key no quedo asociada a una licencia.")

    return {
        "success": True,
        "module_name": MODULE_NAME,
        "module_state": state,
        "module_installed": True,
        "api_key": api_key,
        "api_key_generated": generated,
        "company_count": len(configs),
        "companies_created": created,
        "companies_updated": updated,
    }


def main():
    args = _parser().parse_args()
    request = _load_request(args.request)
    mode = str(request.get("mode") or "").strip()
    if mode not in {"status", "bootstrap"}:
        raise RuntimeError("Operacion de licencias no soportada.")

    Environment, Registry, superuser_id = _environment(args)
    with Registry.new(args.database).cursor() as cursor:
        env = Environment(cursor, superuser_id, {})
        if mode == "status":
            state = _module_state(env)
            result = {
                "success": True,
                "module_name": MODULE_NAME,
                "module_state": state,
                "module_installed": state in ("installed", "to upgrade"),
            }
        else:
            result = _bootstrap(env)

    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
