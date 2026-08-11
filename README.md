# Genesis Infrastructure Agent 2.0

El agente ya no publica una API en `0.0.0.0:8010`.

El servidor inicia conexiones HTTPS hacia el Odoo Administrador:

1. Enrolamiento con token de un solo uso.
2. Heartbeat.
3. Solicitud de trabajos.
4. Ejecución de operaciones tipadas.
5. Envío de resultados.

## Trabajos soportados

- `service.status`
- `service.start`
- `service.stop`
- `service.restart`
- `service.logs`
- `provision.prepare`
- `provision.create`

No existe un endpoint para ejecutar comandos shell arbitrarios.

## Instalación

```bash
sudo bash install.sh   --controller https://ADMIN.DOMINIO   --server-code SERV01   --enroll-token TOKEN
```

El token de enrolamiento se borra del YAML después del registro.


## Repositorio de módulos privados

La configuración soporta el esquema usado por Genesis:

```yaml
provision:
  custom_addons_repo: "https://github.com/ORGANIZACION/modulosFE19.git"
  custom_addons_branch: "main"
  custom_addons_subpaths:
    - "custom_addons"
    - "modulos"
```

Si el repositorio es privado, `install.sh --github-token TOKEN` guarda el token
en `/opt/genesis-admin-agent/.env` con permisos 600. Git usa `GIT_ASKPASS`; el
token no se agrega a la URL del repositorio.
