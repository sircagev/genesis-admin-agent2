#!/bin/bash
set -euo pipefail

BASE="/opt/genesis-admin-agent"
REPO_URL="${GENESIS_AGENT_REPO:-https://github.com/sircagev/genesis-admin-agent2.git}"
SERVICE_NAME="genesis-admin-agent.service"

CONTROLLER=""
DATABASE=""
SERVER_CODE=""
ENROLL_TOKEN=""
GITHUB_TOKEN_VALUE=""
FORCE_REENROLL="0"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --controller) CONTROLLER="$2"; shift 2 ;;
        --database) DATABASE="$2"; shift 2 ;;
        --server-code) SERVER_CODE="$2"; shift 2 ;;
        --enroll-token) ENROLL_TOKEN="$2"; shift 2 ;;
        --github-token) GITHUB_TOKEN_VALUE="$2"; shift 2 ;;
        --force-reenroll) FORCE_REENROLL="1"; shift ;;
        *) echo "Argumento desconocido: $1"; exit 2 ;;
    esac
done

if [ -z "$CONTROLLER" ] || [ -z "$DATABASE" ] || [ -z "$SERVER_CODE" ]; then
    echo "Uso:"
    echo "  sudo bash install.sh \\"
    echo "    --controller https://admin.midominio.com \\"
    echo "    --database admin_db \\"
    echo "    --server-code SERV01 \\"
    echo "    --enroll-token TOKEN"
    exit 2
fi

echo "Instalando/actualizando Genesis Infrastructure Agent..."

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    ca-certificates curl git openssl python3 python3-venv \
    build-essential libpq-dev libldap2-dev libsasl2-dev \
    libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev libffi-dev

# Preserve config before resetting the repository.
TMP_CONFIG=""
if [ -f "$BASE/config/config.yaml" ]; then
    TMP_CONFIG="$(mktemp)"
    cp "$BASE/config/config.yaml" "$TMP_CONFIG"
fi

if [ -d "$BASE/.git" ]; then
    echo "Repositorio existente: actualizando..."
    git -C "$BASE" fetch --all --prune
    git -C "$BASE" reset --hard origin/main
else
    rm -rf "$BASE"
    echo "Clonando $REPO_URL ..."
    git clone "$REPO_URL" "$BASE"
fi

cd "$BASE"

python3 -m venv venv
"$BASE/venv/bin/pip" install --upgrade pip wheel
"$BASE/venv/bin/pip" install --upgrade -r requirements.txt

mkdir -p "$BASE/config"

if [ -n "$TMP_CONFIG" ] && [ -f "$TMP_CONFIG" ]; then
    cp "$TMP_CONFIG" "$BASE/config/config.yaml"
    rm -f "$TMP_CONFIG"
elif [ ! -f "$BASE/config/config.yaml" ]; then
    cp "$BASE/config/config.yaml.tpl" "$BASE/config/config.yaml"
fi

# Normalize old/malformed configurations and preserve a valid identity.
"$BASE/venv/bin/python" - \
    "$BASE/config/config.yaml" \
    "$CONTROLLER" \
    "$DATABASE" \
    "$SERVER_CODE" \
    "$ENROLL_TOKEN" \
    "$FORCE_REENROLL" <<'PY'
import sys
from copy import deepcopy
from pathlib import Path

import yaml

path = Path(sys.argv[1])
controller = sys.argv[2].rstrip("/")
database = sys.argv[3]
server_code = sys.argv[4]
enroll_token = sys.argv[5]
force = sys.argv[6] == "1"

defaults = {
    "controller_url": controller,
    "database": database,
    "server_code": server_code,
    "enrollment_token": "",
    "agent_id": "",
    "agent_token": "",
    "verify_tls": True,
    "poll_interval": 3,
    "heartbeat_interval": 30,
    "request_timeout": 30,
    "log_default_lines": 200,
    "allowed_exact": ["nginx", "remote_print"],
    "allowed_prefix": ["odoo-server-"],
    "provision": {
        "base_dir": "/opt",
        "odoo_repo": "https://github.com/odoo/odoo.git",
        "custom_addons_repo": "",
        "custom_addons_branch": "",
        "custom_addons_subpaths": ["custom_addons", "modulos"],
        "certbot_email": "",
        "default_http_interface": "127.0.0.1",
        "default_log_level": "info",
    },
}

try:
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except Exception:
    current = {}

data = deepcopy(defaults)

for key in (
    "verify_tls",
    "poll_interval",
    "heartbeat_interval",
    "request_timeout",
    "log_default_lines",
    "allowed_exact",
    "allowed_prefix",
):
    if key in current and isinstance(current[key], type(defaults[key])):
        data[key] = current[key]

if isinstance(current.get("provision"), dict):
    data["provision"].update(current["provision"])

# Explicit installer values always win.
data["controller_url"] = controller
data["database"] = database
data["server_code"] = server_code

already_enrolled = bool(
    str(current.get("agent_id") or "").strip()
    and str(current.get("agent_token") or "").strip()
)

if already_enrolled and not force:
    data["agent_id"] = current["agent_id"]
    data["agent_token"] = current["agent_token"]
    data["enrollment_token"] = ""
else:
    data["agent_id"] = ""
    data["agent_token"] = ""
    data["enrollment_token"] = enroll_token

path.write_text(
    yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
    encoding="utf-8",
)
path.chmod(0o600)
PY

if [ -n "$GITHUB_TOKEN_VALUE" ]; then
    cat > "$BASE/.env" <<EOF
GITHUB_TOKEN=$GITHUB_TOKEN_VALUE
EOF
    chmod 600 "$BASE/.env"
fi

chmod 600 "$BASE/config/config.yaml"
chown -R root:root "$BASE"

chmod +x "$BASE/bin/genesis-agent"

ln -sfn "$BASE/bin/genesis-agent" /usr/local/bin/genesis-agent

cp "$BASE/systemd/genesis-admin-agent.service.tpl" \
   /etc/systemd/system/genesis-admin-agent.service

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "✓ Instalación/actualización completada."
echo
echo "Comandos disponibles:"
echo "  sudo genesis-agent status"
echo "  sudo genesis-agent doctor"
echo "  sudo genesis-agent logs"
echo "  sudo genesis-agent restart"
echo "  sudo genesis-agent update"
echo
echo "Para volver a registrar el servidor:"
echo "  sudo genesis-agent reenroll TOKEN_NUEVO"
echo
systemctl --no-pager --full status "$SERVICE_NAME" || true
