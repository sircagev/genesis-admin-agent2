#!/bin/bash
set -euo pipefail

BASE="/opt/genesis-admin-agent"
REPO_URL="${GENESIS_AGENT_REPO:-https://github.com/sircagev/genesis-admin-agent.git}"

CONTROLLER=""
DATABASE=""
SERVER_CODE=""
ENROLL_TOKEN=""
GITHUB_TOKEN_VALUE=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --controller) CONTROLLER="$2"; shift 2 ;;
        --database) DATABASE="$2"; shift 2 ;;
        --server-code) SERVER_CODE="$2"; shift 2 ;;
        --enroll-token) ENROLL_TOKEN="$2"; shift 2 ;;
        --github-token) GITHUB_TOKEN_VALUE="$2"; shift 2 ;;
        *) echo "Argumento desconocido: $1"; exit 2 ;;
    esac
done

if [ -z "$CONTROLLER" ] || [ -z "$DATABASE" ] || [ -z "$SERVER_CODE" ] || [ -z "$ENROLL_TOKEN" ]; then
    echo "Uso:"
    echo "  sudo bash install.sh --controller https://admin.midominio.com \\"
    echo "    --database admin_db --server-code SERV01 --enroll-token TOKEN"
    exit 2
fi

echo "Instalando Genesis Infrastructure Agent..."

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    ca-certificates curl git openssl python3 python3-venv \
    build-essential libpq-dev libldap2-dev libsasl2-dev \
    libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev libffi-dev

if [ -d "$BASE/.git" ]; then
    git -C "$BASE" fetch --all --prune
    git -C "$BASE" reset --hard origin/main
else
    git clone "$REPO_URL" "$BASE"
fi

cd "$BASE"

python3 -m venv venv
"$BASE/venv/bin/pip" install --upgrade pip wheel
"$BASE/venv/bin/pip" install -r requirements.txt

mkdir -p "$BASE/config"
if [ ! -f "$BASE/config/config.yaml" ]; then
    cp "$BASE/config/config.yaml.tpl" "$BASE/config/config.yaml"
fi

python3 - "$BASE/config/config.yaml" "$CONTROLLER" "$DATABASE" "$SERVER_CODE" "$ENROLL_TOKEN" <<'PY'
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
data["controller_url"] = sys.argv[2].rstrip("/")
data["database"] = sys.argv[3]
data["server_code"] = sys.argv[4]
data["enrollment_token"] = sys.argv[5]
data["agent_id"] = ""
data["agent_token"] = ""
path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
PY

chmod 600 "$BASE/config/config.yaml"
chown -R root:root "$BASE"

if [ -n "$GITHUB_TOKEN_VALUE" ]; then
    cat > "$BASE/.env" <<EOF
GITHUB_TOKEN=$GITHUB_TOKEN_VALUE
EOF
    chmod 600 "$BASE/.env"
fi

cp "$BASE/systemd/genesis-admin-agent.service.tpl" \
   /etc/systemd/system/genesis-admin-agent.service

systemctl daemon-reload
systemctl enable genesis-admin-agent.service
systemctl restart genesis-admin-agent.service

echo
echo "Agente instalado."
echo "No se abrió ningún puerto entrante."
echo
echo "Estado:"
systemctl --no-pager --full status genesis-admin-agent.service || true
echo
echo "Logs:"
echo "journalctl -u genesis-admin-agent.service -f"
