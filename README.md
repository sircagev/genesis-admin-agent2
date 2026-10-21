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
sudo genesis-agent backup daily --dry-run
sudo genesis-agent reenroll TOKEN_NUEVO
```

También puede cambiar parámetros durante el re-enrolamiento:

```bash
sudo genesis-agent reenroll   --controller 'https://admin.midominio.com'   --database 'admin_db'   --server-code 'CONTABO-123'   --token 'TOKEN'
```

## Backups programados

El Agent incluye `genesis-agent backup hourly|daily|weekly`. Descubre las
bases asociadas inequívocamente a servicios Odoo activos y genera ZIPs nativos
de Odoo, con subida/verificación rclone y retención configurables.

La función permanece desactivada por defecto mediante `scheduled_backup.enabled`
para no duplicar un cron existente. Antes de activarla, use:

```bash
sudo genesis-agent backup daily --dry-run
```

Una lista vacía de `scheduled_backup.database_names` selecciona todas las bases
asociadas; una lista con nombres limita el alcance de forma explícita.

## Configuración rclone desde Odoo

El remoto se administra desde Odoo mediante el trabajo `rclone.configure`.
Después de validarlo, el Agent guarda el destino recibido —por ejemplo
`gendrive:odoo-backups`— como `scheduled_backup.rclone_remote`. Las credenciales
nunca se incluyen en el trabajo: se recuperan desde el Controller autenticado.
El estado de activación también proviene del campo **Activar backups** de Odoo;
no se edita manualmente el archivo de configuración del Agent.

Cuando se ejecuta ese trabajo, el Agent comprueba si `rclone` está disponible.
Si falta, en servidores Debian/Ubuntu donde el Agent corre como `root`, ejecuta
`apt-get update` e instala el paquete `rclone`; si ya existe, continúa
directamente con la configuración y validación del remoto.

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
