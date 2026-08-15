import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections import Counter
from pathlib import Path

from .client import ControllerClient
from .commands import (
    CommandError,
    run,
    systemd_status,
)
from .provisioner import OdooProvisioner


DB_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"
)

ROLE_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$"
)

UNIT_RE = re.compile(
    r"^odoo-server-[a-z0-9-]+\.service$"
)

TOKEN_RE = re.compile(
    r"^[A-Za-z0-9_-]{20,200}$"
)


class DatabaseManager:
    CHUNK_SIZE = 768 * 1024

    def __init__(self, config):
        self.config = config

        self.client = ControllerClient(
            config
        )

        self.provisioner = OdooProvisioner(
            config
        )

        self.work_dir = Path(
            (
                config.get("database_transfer_dir")
                or
                "/var/lib/genesis-admin-agent/"
                "database-transfers"
            )
        )

        self.work_dir.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )

    # =========================================================
    # VALIDACIÓN
    # =========================================================

    @staticmethod
    def _validate_database_name(name):
        name = str(
            name
            or ""
        ).strip()

        if not DB_RE.fullmatch(name):
            raise CommandError(
                f"Nombre de base de datos no permitido: {name!r}"
            )

        return name

    @staticmethod
    def _validate_role(role):
        role = str(
            role
            or ""
        ).strip()

        if not ROLE_RE.fullmatch(role):
            raise CommandError(
                f"Rol PostgreSQL no permitido: {role!r}"
            )

        return role

    @staticmethod
    def _validate_unit(unit):
        unit = str(
            unit
            or ""
        ).strip()

        if not UNIT_RE.fullmatch(unit):
            raise CommandError(
                f"Unidad systemd no permitida: {unit!r}"
            )

        return unit

    @staticmethod
    def _validate_token(token):
        token = str(
            token
            or ""
        ).strip()

        if not TOKEN_RE.fullmatch(token):
            raise CommandError(
                "Token de transferencia inválido."
            )

        return token

    @staticmethod
    def _sql_literal(value):
        return "'" + str(value).replace(
            "'",
            "''",
        ) + "'"

    # =========================================================
    # POSTGRESQL
    # =========================================================

    def _database_exists(self, database_name):
        database_name = self._validate_database_name(
            database_name
        )

        sql = (
            "SELECT 1 "
            "FROM pg_database "
            "WHERE datname = "
            f"{self._sql_literal(database_name)};"
        )

        result = run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-At",
                "-d",
                "postgres",
                "-c",
                sql,
            ],
            check=False,
            timeout=30,
        )

        if not result.get("success"):
            raise CommandError(
                "No fue posible consultar PostgreSQL: "
                f"{result.get('output') or ''}"
            )

        return (
            result.get("output")
            or ""
        ).strip() == "1"

    def _role_exists(self, role):
        role = self._validate_role(
            role
        )

        sql = (
            "SELECT 1 "
            "FROM pg_roles "
            "WHERE rolname = "
            f"{self._sql_literal(role)};"
        )

        result = run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-At",
                "-d",
                "postgres",
                "-c",
                sql,
            ],
            check=False,
            timeout=30,
        )

        return (
            result.get("success")
            and (
                result.get("output")
                or ""
            ).strip() == "1"
        )

    def _ensure_role(self, role):
        role = self._validate_role(
            role
        )

        if self._role_exists(role):
            return

        quoted = (
            '"'
            + role.replace('"', '""')
            + '"'
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                "postgres",
                "-c",
                (
                    f"CREATE ROLE {quoted} "
                    "LOGIN CREATEDB;"
                ),
            ],
            timeout=60,
        )

    def _terminate_connections(
        self,
        database_name,
    ):
        database_name = self._validate_database_name(
            database_name
        )

        sql = (
            "SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity "
            "WHERE datname = "
            f"{self._sql_literal(database_name)} "
            "AND pid <> pg_backend_pid();"
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            timeout=60,
        )

    def _drop_database(
        self,
        database_name,
    ):
        database_name = self._validate_database_name(
            database_name
        )

        if not self._database_exists(
            database_name
        ):
            return

        self._terminate_connections(
            database_name
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "dropdb",
                database_name,
            ],
            timeout=120,
        )

    def _create_database(
        self,
        database_name,
        owner,
    ):
        database_name = self._validate_database_name(
            database_name
        )

        owner = self._validate_role(
            owner
        )

        self._ensure_role(
            owner
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "createdb",
                "-O",
                owner,
                database_name,
            ],
            timeout=120,
        )

    # =========================================================
    # SYSTEMD / ODOO CONFIG
    # =========================================================

    def _service_config(
        self,
        unit,
        explicit_config="",
    ):
        unit = self._validate_unit(
            unit
        )

        config_path = str(
            explicit_config
            or ""
        ).strip()

        if config_path:
            path = Path(
                config_path
            )

            if path.is_file():
                return (
                    path,
                    self.provisioner._read_odoo_config(
                        path
                    ),
                )

        status = systemd_status(
            unit
        )

        config_path = (
            self.provisioner
            ._detect_odoo_config(
                unit,
                status,
            )
        )

        if not config_path:
            return (
                None,
                {},
            )

        path = Path(
            config_path
        )

        return (
            path,
            self.provisioner._read_odoo_config(
                path
            ),
        )

    def _systemd_identity(self, unit):
        unit = self._validate_unit(
            unit
        )

        result = run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                "--property=User,Group",
            ],
            check=False,
            timeout=30,
        )

        values = {}

        for line in (
            result.get("output")
            or ""
        ).splitlines():

            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1,
            )

            values[key] = (
                value.strip()
            )

        user = (
            values.get("User")
            or "odoo"
        )

        group = (
            values.get("Group")
            or user
            or "odoo"
        )

        return (
            user,
            group,
        )

    def _resolve_data_dir(
        self,
        config,
        system_user,
        database_name,
        explicit="",
        source=False,
    ):
        explicit = str(
            explicit
            or ""
        ).strip()

        configured = str(
            config.get("data_dir")
            or ""
        ).strip()

        candidates = []

        if explicit:
            candidates.append(
                Path(explicit)
            )

        if configured:
            candidates.append(
                Path(configured)
            )

        try:
            user_home = Path(
                pwd.getpwnam(
                    system_user
                ).pw_dir
            )

            candidates.append(
                user_home
                / ".local"
                / "share"
                / "Odoo"
            )

        except KeyError:
            pass

        candidates.extend(
            [
                Path("/var/lib/odoo/.local/share/Odoo"),
                Path("/var/lib/odoo"),
            ]
        )

        unique = []

        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)

        if source:
            for candidate in unique:
                if (
                    candidate
                    / "filestore"
                    / database_name
                ).is_dir():
                    return candidate

        if explicit:
            return Path(explicit)

        if configured:
            return Path(configured)

        if unique:
            return unique[0]

        return Path(
            "/var/lib/odoo/.local/share/Odoo"
        )

    # =========================================================
    # HASH
    # =========================================================

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()

        with Path(path).open(
            "rb"
        ) as handle:

            while True:
                chunk = handle.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                digest.update(
                    chunk
                )

        return digest.hexdigest()

    # =========================================================
    # DUMP
    # =========================================================

    @staticmethod
    def _pg_dump_to_file(
        database_name,
        target,
    ):
        target = Path(
            target
        )

        cmd = [
            "runuser",
            "-u",
            "postgres",
            "--",
            "pg_dump",
            "-Fc",
            "--no-owner",
            "--no-privileges",
            database_name,
        ]

        try:
            with target.open(
                "wb"
            ) as output:

                process = subprocess.run(
                    cmd,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=7200,
                    check=False,
                )

        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                "Timeout generando pg_dump."
            ) from exc

        if process.returncode != 0:
            error = (
                process.stderr
                or b""
            ).decode(
                "utf-8",
                errors="replace",
            )

            raise CommandError(
                "pg_dump falló: "
                f"{error[-3000:]}"
            )

    # =========================================================
    # BACKUP
    # =========================================================

    def backup(self, payload):
        token = self._validate_token(
            payload.get("transfer_token")
        )

        database_name = self._validate_database_name(
            payload.get("database_name")
        )

        unit = self._validate_unit(
            payload.get("service_name")
        )

        if not self._database_exists(
            database_name
        ):
            raise CommandError(
                f"La base {database_name!r} no existe."
            )

        config_path, config = (
            self._service_config(
                unit,
                payload.get("config_path"),
            )
        )

        system_user, _system_group = (
            self._systemd_identity(
                unit
            )
        )

        data_dir = self._resolve_data_dir(
            config=config,
            system_user=system_user,
            database_name=database_name,
            explicit=payload.get(
                "data_dir"
            ),
            source=True,
        )

        workspace = Path(
            tempfile.mkdtemp(
                prefix="backup-",
                dir=self.work_dir,
            )
        )

        dump_path = (
            workspace
            / "database.dump"
        )

        bundle_path = (
            workspace
            / f"{database_name}.bundle"
        )

        try:
            # -------------------------------------------------
            # 1. PostgreSQL
            # -------------------------------------------------

            self._pg_dump_to_file(
                database_name,
                dump_path,
            )

            # -------------------------------------------------
            # 2. Manifest
            # -------------------------------------------------

            manifest = {
                "format": 1,
                "database_name": database_name,
                "odoo_version": (
                    payload.get("odoo_version")
                    or ""
                ),
                "source_service": unit,
                "source_config": (
                    str(config_path)
                    if config_path
                    else ""
                ),
                "source_data_dir": str(
                    data_dir
                ),
            }

            manifest_path = (
                workspace
                / "manifest.json"
            )

            manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            # -------------------------------------------------
            # 3. Bundle: DB + filestore
            # -------------------------------------------------

            source_filestore = (
                data_dir
                / "filestore"
                / database_name
            )

            with tarfile.open(
                bundle_path,
                "w:gz",
            ) as archive:

                archive.add(
                    dump_path,
                    arcname="database.dump",
                    recursive=False,
                )

                archive.add(
                    manifest_path,
                    arcname="manifest.json",
                    recursive=False,
                )

                if source_filestore.is_dir():
                    archive.add(
                        source_filestore,
                        arcname="filestore",
                    )

            size = (
                bundle_path
                .stat()
                .st_size
            )

            sha256 = self._sha256(
                bundle_path
            )

            # -------------------------------------------------
            # 4. Inicializar transferencia
            # -------------------------------------------------

            begin = self.client.transfer_begin(
                token=token,
                file_name=bundle_path.name,
                size=size,
                sha256=sha256,
            )

            offset = int(
                begin.get("offset")
                or 0
            )

            # -------------------------------------------------
            # 5. Upload en fragmentos pequeños
            # -------------------------------------------------

            with bundle_path.open(
                "rb"
            ) as handle:

                handle.seek(offset)

                while True:
                    chunk = handle.read(
                        self.CHUNK_SIZE
                    )

                    if not chunk:
                        break

                    response = (
                        self.client
                        .transfer_upload(
                            token,
                            offset,
                            chunk,
                        )
                    )

                    offset = int(
                        response.get(
                            "offset"
                        )
                        or (
                            offset
                            + len(chunk)
                        )
                    )

            # -------------------------------------------------
            # 6. Verificación controller
            # -------------------------------------------------

            completed = (
                self.client
                .transfer_complete(
                    token,
                    size,
                    sha256,
                )
            )

            if not completed.get(
                "success"
            ):
                raise CommandError(
                    "El controlador no confirmó "
                    "la transferencia."
                )

            return {
                "success": True,
                "message": (
                    f"Backup de {database_name} "
                    "generado y transferido."
                ),
                "transfer_uploaded": True,
                "database_name": database_name,
                "file_name": bundle_path.name,
                "file_size": size,
                "sha256": sha256,
                "filestore_included": (
                    source_filestore.is_dir()
                ),
                "data_dir": str(
                    data_dir
                ),
            }

        finally:
            shutil.rmtree(
                workspace,
                ignore_errors=True,
            )

    # =========================================================
    # EXTRAER BUNDLE
    # =========================================================

    @staticmethod
    def _safe_extract(
        bundle_path,
        target_dir,
    ):
        target_dir = Path(
            target_dir
        ).resolve()

        with tarfile.open(
            bundle_path,
            "r:gz",
        ) as archive:

            members = archive.getmembers()

            for member in members:
                if (
                    member.issym()
                    or member.islnk()
                ):
                    raise CommandError(
                        "El bundle contiene enlaces no permitidos."
                    )

                destination = (
                    target_dir
                    / member.name
                ).resolve()

                if (
                    destination != target_dir
                    and target_dir not in destination.parents
                ):
                    raise CommandError(
                        "El bundle contiene rutas no permitidas."
                    )

            archive.extractall(
                target_dir
            )

    # =========================================================
    # PARAMETRIZACIÓN DE LA BD CLONADA
    # =========================================================

    def _sanitize_cloned_database(
        self,
        database_name,
        target_domain,
    ):
        database_uuid = str(
            uuid.uuid4()
        )

        database_secret = (
            secrets.token_urlsafe(
                48
            )
        )

        statements = []

        def upsert(key, value):
            statements.append(
                """
                INSERT INTO ir_config_parameter
                    (
                        key,
                        value,
                        create_uid,
                        create_date,
                        write_uid,
                        write_date
                    )
                VALUES
                    (
                        %(key)s,
                        %(value)s,
                        1,
                        NOW(),
                        1,
                        NOW()
                    )
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    write_uid = 1,
                    write_date = NOW();
                """
                % {
                    "key": self._sql_literal(
                        key
                    ),
                    "value": self._sql_literal(
                        value
                    ),
                }
            )

        upsert(
            "database.uuid",
            database_uuid,
        )

        upsert(
            "database.secret",
            database_secret,
        )

        target_domain = str(
            target_domain
            or ""
        ).strip().lower()

        if target_domain:
            upsert(
                "web.base.url",
                f"https://{target_domain}",
            )

        statements.append(
            """
            DELETE FROM ir_config_parameter
            WHERE key = 'web.base.url.freeze';
            """
        )

        sql = "\n".join(
            statements
        )

        run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                database_name,
                "-c",
                sql,
            ],
            timeout=120,
        )

    # =========================================================
    # INVENTARIO BD
    # =========================================================

    def _database_info(
        self,
        database_name,
        unit,
        config,
        data_dir,
        system_user,
    ):
        sql = (
            "SELECT "
            "d.datname, "
            "pg_get_userbyid(d.datdba), "
            "pg_database_size(d.datname), "
            "pg_encoding_to_char(d.encoding), "
            "d.datcollate, "
            "d.datctype "
            "FROM pg_database d "
            "WHERE d.datname = "
            f"{self._sql_literal(database_name)};"
        )

        result = run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-At",
                "-F",
                "\t",
                "-d",
                "postgres",
                "-c",
                sql,
            ],
            timeout=60,
        )

        line = (
            result.get("output")
            or ""
        ).strip()

        if not line:
            return {
                "name": database_name,
                "service_unit": unit,
                "system_user": system_user,
                "data_dir": str(data_dir),
            }

        parts = line.split(
            "\t"
        )

        while len(parts) < 6:
            parts.append("")

        size_bytes = int(
            parts[2]
            or 0
        )

        return {
            "name": parts[0],
            "owner": parts[1],
            "size_mb": round(
                size_bytes
                / 1024
                / 1024,
                2,
            ),
            "encoding": parts[3],
            "collation": parts[4],
            "ctype": parts[5],
            "service_unit": unit,
            "service_technical_name": (
                unit
                .replace(
                    "odoo-server-",
                    "",
                    1,
                )
                .removesuffix(
                    ".service"
                )
            ),
            "odoo_version": (
                config.get(
                    "_odoo_version"
                )
                or ""
            ),
            "system_user": system_user,
            "data_dir": str(
                data_dir
            ),
        }

    # =========================================================
    # RESTAURACIÓN
    # =========================================================

    def restore(self, payload):
        token = self._validate_token(
            payload.get("transfer_token")
        )

        database_name = self._validate_database_name(
            payload.get("database_name")
        )

        database_owner = self._validate_role(
            payload.get("database_owner")
        )

        unit = self._validate_unit(
            payload.get("service_name")
        )

        replace_existing = bool(
            payload.get(
                "replace_existing",
                False,
            )
        )

        target_domain = str(
            payload.get("target_domain")
            or ""
        ).strip()

        existing = self._database_exists(
            database_name
        )

        if (
            existing
            and not replace_existing
        ):
            raise CommandError(
                f"La base {database_name!r} ya existe. "
                "Por seguridad no será reemplazada."
            )

        config_path, config = (
            self._service_config(
                unit,
                payload.get("config_path"),
            )
        )

        system_user, system_group = (
            self._systemd_identity(
                unit
            )
        )

        data_dir = self._resolve_data_dir(
            config=config,
            system_user=system_user,
            database_name=database_name,
            source=False,
        )

        workspace = Path(
            tempfile.mkdtemp(
                prefix="restore-",
                dir=self.work_dir,
            )
        )

        bundle_path = (
            workspace
            / "database.bundle"
        )

        extract_dir = (
            workspace
            / "extracted"
        )

        extract_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        created_database = False
        created_filestore = False

        status_before = systemd_status(
            unit
        )

        was_active = (
            status_before.get(
                "active_state"
            )
            == "active"
        )

        try:
            # -------------------------------------------------
            # 1. Descargar desde controller
            # -------------------------------------------------

            self.client.transfer_download(
                token,
                bundle_path,
            )

            # -------------------------------------------------
            # 2. Verificar/extract
            # -------------------------------------------------

            self._safe_extract(
                bundle_path,
                extract_dir,
            )

            dump_path = (
                extract_dir
                / "database.dump"
            )

            if not dump_path.is_file():
                raise CommandError(
                    "El bundle no contiene database.dump."
                )

            manifest_path = (
                extract_dir
                / "manifest.json"
            )

            if not manifest_path.is_file():
                raise CommandError(
                    "El bundle no contiene manifest.json."
                )

            # -------------------------------------------------
            # 3. Detener solamente la instancia destino
            # -------------------------------------------------

            run(
                [
                    "systemctl",
                    "stop",
                    unit,
                ],
                timeout=120,
            )

            # -------------------------------------------------
            # 4. PostgreSQL
            # -------------------------------------------------

            if existing:
                self._drop_database(
                    database_name
                )

            self._create_database(
                database_name,
                database_owner,
            )

            created_database = True

            run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--role",
                    database_owner,
                    "-d",
                    database_name,
                    str(dump_path),
                ],
                timeout=7200,
            )

            # -------------------------------------------------
            # 5. No conservar UUID/secret/url de la plantilla
            # -------------------------------------------------

            self._sanitize_cloned_database(
                database_name,
                target_domain,
            )

            # -------------------------------------------------
            # 6. FILESTORE
            # -------------------------------------------------

            extracted_filestore = (
                extract_dir
                / "filestore"
            )

            target_filestore = (
                data_dir
                / "filestore"
                / database_name
            )

            if extracted_filestore.is_dir():

                if target_filestore.exists():
                    if not replace_existing:
                        raise CommandError(
                            "El filestore destino ya existe."
                        )

                    shutil.rmtree(
                        target_filestore
                    )

                target_filestore.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                shutil.copytree(
                    extracted_filestore,
                    target_filestore,
                )

                created_filestore = True

                run(
                    [
                        "chown",
                        "-R",
                        (
                            f"{system_user}:"
                            f"{system_group}"
                        ),
                        str(target_filestore),
                    ],
                    timeout=600,
                )

            # -------------------------------------------------
            # 7. Asegurar dueño de la BD
            # -------------------------------------------------

            quoted_owner = (
                '"'
                + database_owner.replace(
                    '"',
                    '""',
                )
                + '"'
            )

            quoted_database = (
                '"'
                + database_name.replace(
                    '"',
                    '""',
                )
                + '"'
            )

            run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-d",
                    "postgres",
                    "-c",
                    (
                        f"ALTER DATABASE "
                        f"{quoted_database} "
                        f"OWNER TO "
                        f"{quoted_owner};"
                    ),
                ],
                timeout=60,
            )

            # -------------------------------------------------
            # 8. Iniciar Odoo
            # -------------------------------------------------

            run(
                [
                    "systemctl",
                    "start",
                    unit,
                ],
                timeout=120,
            )

            database_info = self._database_info(
                database_name=database_name,
                unit=unit,
                config=config,
                data_dir=data_dir,
                system_user=system_user,
            )

            return {
                "success": True,
                "message": (
                    f"Base {database_name} "
                    "restaurada correctamente."
                ),
                "database_name": database_name,
                "database": database_info,
                "config_path": (
                    str(config_path)
                    if config_path
                    else ""
                ),
                "data_dir": str(
                    data_dir
                ),
                "filestore_restored": (
                    created_filestore
                ),
            }

        except Exception:
            # Para una creación NUEVA no dejamos una BD
            # parcialmente restaurada.
            if (
                created_database
                and not existing
            ):
                try:
                    self._drop_database(
                        database_name
                    )
                except Exception:
                    pass

            if created_filestore:
                try:
                    target_filestore = (
                        data_dir
                        / "filestore"
                        / database_name
                    )

                    shutil.rmtree(
                        target_filestore,
                        ignore_errors=True,
                    )
                except Exception:
                    pass

            raise

        finally:
            # Si el servicio estaba activo antes de empezar,
            # procuramos dejarlo nuevamente activo incluso si
            # la restauración falló.
            if was_active:
                try:
                    run(
                        [
                            "systemctl",
                            "start",
                            unit,
                        ],
                        check=False,
                        timeout=120,
                    )
                except Exception:
                    pass

            shutil.rmtree(
                workspace,
                ignore_errors=True,
            )