[Unit]
Description=Genesis Infrastructure Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/genesis-admin-agent
EnvironmentFile=-/opt/genesis-admin-agent/.env
ExecStart=/opt/genesis-admin-agent/venv/bin/python -m agent.main
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
