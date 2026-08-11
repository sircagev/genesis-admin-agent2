# Genesis Infrastructure Agent 2.1.0

El agente inicia conexiones HTTPS hacia el Administrador Odoo. No abre un
puerto administrativo entrante.

## Comando administrativo

El instalador crea:

```text
/usr/local/bin/genesis-agent
```

Comandos:

```bash
sudo genesis-agent status
sudo genesis-agent doctor
sudo genesis-agent logs
sudo genesis-agent restart
sudo genesis-agent update
sudo genesis-agent reenroll TOKEN_NUEVO
```

También puede cambiar parámetros durante el re-enrolamiento:

```bash
sudo genesis-agent reenroll   --controller 'https://admin.midominio.com'   --database 'admin_db'   --server-code 'CONTABO-123'   --token 'TOKEN'
```

## Instalador idempotente

`install.sh` se puede ejecutar nuevamente. Si la instalación ya tiene
`agent_id` y `agent_token`, conserva la identidad y solo actualiza código,
dependencias, configuración base y systemd.

Para forzar un nuevo registro desde el instalador:

```bash
sudo bash install.sh ... --force-reenroll
```

Para recuperación normal se recomienda `genesis-agent reenroll`.

## Multi-base de Odoo

Todas las llamadas envían:

```text
X-Odoo-Database: <database>
```

No se utiliza `?db=`.
