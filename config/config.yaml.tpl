controller_url: "https://ADMIN-DOMAIN"
database: "ADMIN-DATABASE"
server_code: "SERVER-CODE"
enrollment_token: "ENROLLMENT-TOKEN"

agent_id: ""
agent_token: ""

verify_tls: true
poll_interval: 3
heartbeat_interval: 30
service_sync_interval: 300
request_timeout: 30
log_default_lines: 200

allowed_exact:
  - nginx
  - remote_print

allowed_prefix:
  - odoo-server-

provision:
  base_dir: "/opt"
  odoo_repo: "https://github.com/odoo/odoo.git"
  custom_addons_repo: ""
  custom_addons_branch: ""
  custom_addons_subpaths:
    - "custom_addons"
    - "modulos"
  certbot_email: ""
  default_http_interface: "127.0.0.1"
  default_log_level: "info"
