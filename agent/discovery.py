import re
import shlex
from collections import  Counter, defaultdict
from pathlib import Path

from .commands import (
    port_available,
    run,
    systemd_status,
)
from .provisioner import OdooProvisioner


class OdooServiceDiscovery:
    """Read-only discovery of installed Odoo systemd services and Nginx domains."""

    def __init__(self, config):
        self.config = config
        self.provisioner = OdooProvisioner(config)
        self.allowed_exact = set(config.get("allowed_exact") or [])
        self.allowed_prefix = tuple(config.get("allowed_prefix") or [])

    def discover(self):
        nginx_inventory = self._nginx_inventory()
        services = []

        for unit in self._candidate_units():
            props = self._unit_properties(unit)

            exec_start = (
                props.get("ExecStart")
                or ""
            )

            if not self._looks_like_odoo(
                unit,
                exec_start,
            ):
                continue

            status = systemd_status(unit)

            config_path = None
            config = {}
            config_error = ""

            # -------------------------------------------------
            # DETECTAR ODOO.CONF
            # -------------------------------------------------

            try:
                config_path = (
                    self.provisioner
                    ._detect_odoo_config(
                        unit,
                        status,
                    )
                )

                if config_path:
                    config = (
                        self.provisioner
                        ._read_odoo_config(
                            config_path
                        )
                    )

            except Exception as exc:
                # pylint: disable=broad-except
                config_error = str(exc)

            # -------------------------------------------------
            # PUERTOS
            #
            # Odoo 19:
            #   http_port
            #   gevent_port
            #
            # Odoo 17:
            #   xmlrpc_port
            #   longpolling_port
            # -------------------------------------------------

            http_port = self._as_int(
                config.get("http_port")
                or config.get("xmlrpc_port")
            )

            gevent_port = self._as_int(
                config.get("gevent_port")
                or config.get(
                    "longpolling_port"
                )
            )

            # -------------------------------------------------
            # OWNER / NOMBRE TÉCNICO
            # -------------------------------------------------

            owner = ""

            try:
                owner = (
                    self.provisioner
                    .owner_from_unit(
                        unit
                    )
                )

            except Exception:
                # pylint: disable=broad-except
                owner = (
                    props.get("User")
                    or ""
                ).strip()

            # -------------------------------------------------
            # DOMINIOS
            # -------------------------------------------------

            domains = []

            def add_domains(values):
                for domain in values or []:
                    domain = (
                        str(domain)
                        .strip()
                        .lower()
                        .rstrip(".")
                    )

                    if (
                        domain
                        and domain not in domains
                    ):
                        domains.append(domain)

            # ---------------------------------------------
            # 1. BUSCAR POR PUERTO HTTP
            # ---------------------------------------------

            if http_port:
                add_domains(
                    nginx_inventory[
                        "by_port"
                    ].get(
                        http_port,
                        [],
                    )
                )

            # ---------------------------------------------
            # 2. BUSCAR POR PUERTO GEVENT
            # ---------------------------------------------

            if gevent_port:
                add_domains(
                    nginx_inventory[
                        "by_port"
                    ].get(
                        gevent_port,
                        [],
                    )
                )

            # ---------------------------------------------
            # 3. FALLBACK POR NOMBRE DEL UPSTREAM
            #
            # superdev19
            #    ↓
            # odoosuperdev19
            #
            # pruebas
            #    ↓
            # odoopruebas
            # ---------------------------------------------

            if owner:
                nginx_owner = re.sub(
                    r"[^a-zA-Z0-9]",
                    "",
                    owner,
                ).lower()

                upstream_candidates = (
                    f"odoo{nginx_owner}",
                    f"odoo_{nginx_owner}",
                    f"odoo-{nginx_owner}",
                )

                for upstream in (
                    upstream_candidates
                ):
                    add_domains(
                        nginx_inventory[
                            "by_upstream"
                        ].get(
                            upstream,
                            [],
                        )
                    )

            # -------------------------------------------------
            # DETECTAR VERSIÓN ODOO
            # -------------------------------------------------

            version = (
                self._detect_odoo_version(
                    exec_start=exec_start,
                    working_directory=(
                        props.get(
                            "WorkingDirectory"
                        )
                        or ""
                    ),
                    config_path=config_path,
                )
            )

            # -------------------------------------------------
            # RESULTADO DEL SERVICIO
            # -------------------------------------------------

            service = {
                "unit": unit,

                "technical_name": (
                    owner
                    or self._unit_slug(unit)
                ),

                "user": (
                    props.get("User")
                    or ""
                ),

                "working_directory": (
                    props.get(
                        "WorkingDirectory"
                    )
                    or ""
                ),

                "fragment_path": (
                    props.get(
                        "FragmentPath"
                    )
                    or ""
                ),

                "exec_start": exec_start,

                "active_state": (
                    status.get(
                        "active_state"
                    )
                    or "unknown"
                ),

                "sub_state": (
                    status.get(
                        "sub_state"
                    )
                    or "unknown"
                ),

                "main_pid": (
                    status.get(
                        "main_pid"
                    )
                    or 0
                ),

                "config_path": (
                    str(config_path)
                    if config_path
                    else ""
                ),

                "config_error": (
                    config_error
                ),

                "odoo_version": (
                    version
                ),

                "http_port": (
                    http_port
                    or 0
                ),

                "gevent_port": (
                    gevent_port
                    or 0
                ),

                "workers": self._as_int(
                    config.get("workers"),
                    default=0,
                ),

                "max_cron_threads":
                    self._as_int(
                        config.get(
                            "max_cron_threads"
                        ),
                        default=0,
                    ),

                "http_interface": (
                    config.get(
                        "http_interface"
                    )
                    or config.get(
                        "xmlrpc_interface"
                    )
                    or "127.0.0.1"
                ),

                "proxy_mode":
                    self._as_bool(
                        config.get(
                            "proxy_mode"
                        ),
                        True,
                    ),

                "logfile": (
                    config.get("logfile")
                    or ""
                ),

                "log_level": (
                    config.get(
                        "log_level"
                    )
                    or "info"
                ),

                "db_user": (
                    config.get(
                        "db_user"
                    )
                    or ""
                ),

                "db_name": (
                    config.get(
                        "db_name"
                    )
                    or ""
                ),

                "dbfilter": (
                    config.get(
                        "dbfilter"
                    )
                    or ""
                ),

                "data_dir": (
                    config.get(
                        "data_dir"
                    )
                    or ""
                ),

                "addons_path": (
                    config.get(
                        "addons_path"
                    )
                    or ""
                ),

                "domains": domains,

                "primary_domain": (
                    domains[0]
                    if domains
                    else ""
                ),

                "control_allowed":
                    self._unit_allowed(
                        unit
                    ),
            }

            services.append(
                service
            )

        services.sort(
            key=lambda item: item["unit"]
        )

        return {
            "success": True,

            "message": (
                f"Se detectaron "
                f"{len(services)} "
                f"servicios Odoo."
            ),

            "services": services,

            "count": len(services),
        }

    def _candidate_units(self):
        result = run(
            [
                "systemctl",
                "list-unit-files",
                "--type=service",
                "--no-legend",
                "--no-pager",
            ],
            check=False,
            timeout=30,
        )
        units = []
        for raw in result.get("output", "").splitlines():
            parts = raw.split()
            if not parts:
                continue
            unit = parts[0].strip()
            if not unit.endswith(".service"):
                continue

            lowered = unit.lower()
            if "odoo" in lowered or any(
                lowered.startswith(prefix.lower())
                for prefix in self.allowed_prefix
            ):
                units.append(unit)

        return sorted(set(units))

    @staticmethod
    def _unit_properties(unit):
        result = run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                "--property=ExecStart,User,WorkingDirectory,FragmentPath",
            ],
            check=False,
            timeout=15,
        )
        values = {}
        for line in result.get("output", "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def _looks_like_odoo(self, unit, exec_start):
        lowered = f"{unit} {exec_start}".lower()
        return (
            "odoo-bin" in lowered
            or " -m odoo" in lowered
            or unit.startswith(self.allowed_prefix)
        )

    def _unit_allowed(self, unit):
        if unit in self.allowed_exact:
            return True
        return bool(
            self.allowed_prefix
            and unit.startswith(self.allowed_prefix)
        )

    @staticmethod
    def _unit_slug(unit):
        value = unit[:-8] if unit.endswith(".service") else unit
        value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value)
        return value.strip("-").lower()

    @staticmethod
    def _as_int(value, default=None):
        if value in (None, ""):
            return default
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_bool(value, default=False):
        if value in (None, ""):
            return default
        return str(value).strip().lower() in {
            "1", "true", "yes", "on", "y", "si", "sí"
        }

    def _detect_odoo_version(self, exec_start, working_directory, config_path):
        candidates = []

        try:
            args = shlex.split(exec_start)
        except ValueError:
            args = exec_start.split()

        for value in args:
            if value.endswith("odoo-bin"):
                candidates.append(Path(value).parent / "odoo/release.py")

        if working_directory:
            root = Path(working_directory)
            candidates.extend(
                [
                    root / "odoo/release.py",
                    root / "odoo-server/odoo/release.py",
                ]
            )

        if config_path:
            parent = Path(config_path).parent
            candidates.extend(
                [
                    parent / "odoo-server/odoo/release.py",
                    parent / "odoo/release.py",
                ]
            )

        seen = set()
        for path in candidates:
            path = path.resolve() if path.exists() else path
            if str(path) in seen or not path.is_file():
                continue
            seen.add(str(path))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            match = re.search(
                r"version_info\s*=\s*\(\s*(\d+)\s*,\s*(\d+)",
                text,
            )
            if match:
                return match.group(1)

            match = re.search(
                r"""version\s*=\s*['"](\d+)(?:\.\d+)?""",
                text,
            )
            if match:
                return match.group(1)

        return ""

    def _nginx_inventory(self):
        result = run(
            ["nginx", "-T"],
            check=False,
            timeout=30,
        )

        text = result.get("output") or ""

        if not text:
            return {
                "by_port": {},
                "by_upstream": {},
            }

        # ---------------------------------------------------------
        # QUITAR COMENTARIOS DE NGINX
        # ---------------------------------------------------------

        clean_lines = []

        for raw_line in text.splitlines():
            output = []

            in_single = False
            in_double = False
            escape = False

            for char in raw_line:

                if escape:
                    output.append(char)
                    escape = False
                    continue

                if char == "\\":
                    output.append(char)
                    escape = True
                    continue

                if char == "'" and not in_double:
                    in_single = not in_single
                    output.append(char)
                    continue

                if char == '"' and not in_single:
                    in_double = not in_double
                    output.append(char)
                    continue

                if (
                    char == "#"
                    and not in_single
                    and not in_double
                ):
                    break

                output.append(char)

            clean_lines.append(
                "".join(output)
            )

        text = "\n".join(
            clean_lines
        )

        # ---------------------------------------------------------
        # UPSTREAM -> PUERTOS
        #
        # upstream odoosuperdev19 {
        #     server 127.0.0.1:8079;
        # }
        # ---------------------------------------------------------

        upstream_ports = {}

        upstream_pattern = re.compile(
            r"\bupstream\s+"
            r"([^\s{]+)"
            r"\s*\{"
            r"([^{}]*)"
            r"\}",
            flags=re.I | re.S,
        )

        for match in upstream_pattern.finditer(
            text
        ):
            upstream_name = (
                match.group(1)
                .strip()
                .lower()
            )

            block = match.group(2)

            ports = []

            for port_match in re.finditer(
                r"\bserver\s+"
                r"(?:127\.0\.0\.1|localhost)"
                r":(\d+)\b",
                block,
                flags=re.I,
            ):
                port = int(
                    port_match.group(1)
                )

                if port not in ports:
                    ports.append(port)

            if ports:
                upstream_ports[
                    upstream_name
                ] = ports

        # ---------------------------------------------------------
        # RESULTADOS
        # ---------------------------------------------------------

        by_port = defaultdict(list)
        by_upstream = defaultdict(list)

        # ---------------------------------------------------------
        # LOCALIZAR TODOS LOS SERVER {
        # ---------------------------------------------------------

        server_start_pattern = re.compile(
            r"\bserver\s*\{",
            flags=re.I,
        )

        server_starts = [
            match.start()
            for match
            in server_start_pattern.finditer(
                text
            )
        ]

        # ---------------------------------------------------------
        # SERVER_NAME
        # ---------------------------------------------------------

        server_name_pattern = re.compile(
            r"\bserver_name\s+([^;]+);",
            flags=re.I,
        )

        # ---------------------------------------------------------
        # PROXY_PASS
        # ---------------------------------------------------------

        proxy_pattern = re.compile(
            r"\bproxy_pass\s+"
            r"https?://"
            r"([^/;\s]+)",
            flags=re.I,
        )

        # ---------------------------------------------------------
        # RECORRER CADA PROXY_PASS
        # ---------------------------------------------------------

        for proxy_match in (
            proxy_pattern.finditer(text)
        ):

            proxy_position = (
                proxy_match.start()
            )

            # ---------------------------------------------
            # Buscar el último:
            #
            # server {
            #
            # que aparece antes del proxy_pass actual.
            # ---------------------------------------------

            server_start = None

            for position in server_starts:

                if position > proxy_position:
                    break

                server_start = position

            if server_start is None:
                continue

            # ---------------------------------------------
            # Desde ese server { hasta el proxy_pass
            # buscamos su server_name.
            # ---------------------------------------------

            server_prefix = text[
                server_start:
                proxy_position
            ]

            name_matches = list(
                server_name_pattern.finditer(
                    server_prefix
                )
            )

            if not name_matches:
                continue

            # Tomamos el server_name más cercano
            # al proxy_pass.
            raw_names = (
                name_matches[-1]
                .group(1)
            )

            domains = []

            for value in raw_names.split():

                domain = (
                    value
                    .strip()
                    .lower()
                    .rstrip(".")
                )

                if (
                    self._valid_domain(
                        domain
                    )
                    and domain not in domains
                ):
                    domains.append(
                        domain
                    )

            if not domains:
                continue

            # ---------------------------------------------
            # TARGET
            #
            # Ejemplos:
            #
            # odoosuperdev19
            # odoochatsuperdev19
            # 127.0.0.1:8079
            # ---------------------------------------------

            target = (
                proxy_match
                .group(1)
                .strip()
                .lower()
            )

            # ---------------------------------------------
            # PROXY DIRECTO A PUERTO
            # ---------------------------------------------

            direct = re.fullmatch(
                r"(?:127\.0\.0\.1|localhost)"
                r":(\d+)",
                target,
                flags=re.I,
            )

            if direct:

                port = int(
                    direct.group(1)
                )

                for domain in domains:

                    if (
                        domain
                        not in by_port[port]
                    ):
                        by_port[
                            port
                        ].append(
                            domain
                        )

                continue

            # ---------------------------------------------
            # PROXY A UPSTREAM
            # ---------------------------------------------

            upstream = (
                target
                .split(":", 1)[0]
            )

            for domain in domains:

                if (
                    domain
                    not in by_upstream[
                        upstream
                    ]
                ):
                    by_upstream[
                        upstream
                    ].append(
                        domain
                    )

            # ---------------------------------------------
            # UPSTREAM -> PUERTO -> DOMINIO
            # ---------------------------------------------

            for port in upstream_ports.get(
                upstream,
                [],
            ):

                for domain in domains:

                    if (
                        domain
                        not in by_port[
                            port
                        ]
                    ):
                        by_port[
                            port
                        ].append(
                            domain
                        )

        return {
            "by_port": dict(
                by_port
            ),
            "by_upstream": dict(
                by_upstream
            ),
        }

    @staticmethod
    def _valid_domain(value):
        if (
            not value
            or value == "_"
            or value == "localhost"
            or value.startswith("~")
            or "$" in value
            or "*" in value
        ):
            return False
        return bool(
            re.fullmatch(
                r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z]{2,63}",
                value,
            )
        )

    @staticmethod
    def _named_blocks(text, keyword, named=True):
        if named:
            pattern = re.compile(
                rf"\b{re.escape(keyword)}\s+([^\s{{]+)\s*\{{",
                flags=re.I,
            )
        else:
            pattern = re.compile(
                rf"\b{re.escape(keyword)}\s*\{{",
                flags=re.I,
            )

        results = []
        position = 0

        while True:
            match = pattern.search(text, position)
            if not match:
                break

            name = match.group(1) if named else ""
            open_pos = text.find("{", match.start())
            if open_pos < 0:
                break

            depth = 0
            end_pos = None
            in_single = False
            in_double = False
            escape = False

            for index in range(open_pos, len(text)):
                char = text[index]

                if escape:
                    escape = False
                    continue

                if char == "\\":
                    escape = True
                    continue

                if char == "'" and not in_double:
                    in_single = not in_single
                    continue

                if char == '"' and not in_single:
                    in_double = not in_double
                    continue

                if in_single or in_double:
                    continue

                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end_pos = index
                        break

            if end_pos is None:
                break

            results.append((name, text[open_pos + 1:end_pos]))
            position = end_pos + 1

        return results

    def discover_ports(self, start_port=8069, end_port=9000, pair_limit=30):
        start_port = int(start_port or 8069)
        end_port = int(end_port or 9000)
        pair_limit = int(pair_limit or 30)

        if start_port < 1024:
            start_port = 1024

        if end_port > 65535:
            end_port = 65535

        if end_port <= start_port:
            raise ValueError(
                "El puerto final debe ser mayor "
                "que el puerto inicial."
            )

        # -----------------------------------------------------
        # SERVICIOS ODOO YA CONFIGURADOS
        # -----------------------------------------------------

        discovered = self.discover()

        services = (
            discovered.get("services")
            or []
        )

        reserved = {}

        def reserve(
            port,
            kind,
            service,
        ):
            port = int(port or 0)

            if (
                port < start_port
                or port > end_port
            ):
                return

            reserved.setdefault(
                port,
                [],
            ).append(
                {
                    "source": "odoo",
                    "kind": kind,
                    "unit": (
                        service.get("unit")
                        or ""
                    ),
                    "service": (
                        service.get(
                            "technical_name"
                        )
                        or ""
                    ),
                    "domain": (
                        service.get(
                            "primary_domain"
                        )
                        or ""
                    ),
                }
            )

        for service in services:

            reserve(
                service.get("http_port"),
                "http",
                service,
            )

            reserve(
                service.get("gevent_port"),
                "gevent",
                service,
            )

        # -----------------------------------------------------
        # PUERTOS TCP ESCUCHANDO REALMENTE
        # -----------------------------------------------------

        listening = (
            self._listening_tcp_ports(
                start_port,
                end_port,
            )
        )

        used = (
            set(reserved.keys())
            | listening
        )

        # -----------------------------------------------------
        # PUERTOS LIBRES
        # -----------------------------------------------------

        available = [
            port
            for port
            in range(
                start_port,
                end_port + 1,
            )
            if port not in used
        ]

        available_set = set(
            available
        )

        # -----------------------------------------------------
        # PARES CONSECUTIVOS
        #
        # HTTP / GEVENT
        # -----------------------------------------------------

        pairs = []

        port = start_port

        while (
            port < end_port
            and len(pairs) < pair_limit
        ):
            if (
                port in available_set
                and (
                    port + 1
                ) in available_set
            ):
                pairs.append(
                    {
                        "http_port": port,
                        "gevent_port": (
                            port + 1
                        ),
                    }
                )

                # No devolver pares solapados.
                port += 2

            else:
                port += 1

        # -----------------------------------------------------
        # PUERTOS OCUPADOS + RAZÓN
        # -----------------------------------------------------

        used_ports = []

        for port in sorted(used):

            reasons = list(
                reserved.get(
                    port,
                    [],
                )
            )

            if port in listening:
                reasons.append(
                    {
                        "source": "linux",
                        "kind": "listening",
                        "unit": "",
                        "service": "",
                        "domain": "",
                    }
                )

            used_ports.append(
                {
                    "port": port,
                    "listening": (
                        port in listening
                    ),
                    "reserved": (
                        port in reserved
                    ),
                    "reasons": reasons,
                }
            )

        recommended = (
            pairs[0]
            if pairs
            else {}
        )

        return {
            "success": True,

            "message": (
                f"Puertos "
                f"{start_port}-{end_port} "
                f"revisados. "
                f"{len(available)} libres."
            ),

            "scan_start": start_port,

            "scan_end": end_port,

            "available_ports": (
                available
            ),

            "available_pairs": pairs,

            "recommended_pair": (
                recommended
            ),

            "used_ports": (
                used_ports
            ),

            "used_count": len(used),

            "available_count": (
                len(available)
            ),
        }


    def _listening_tcp_ports(
        self,
        start_port,
        end_port,
    ):
        ports = set()

        # -----------------------------------------------------
        # PRIMER MÉTODO: ss
        #
        # Detecta IPv4, IPv6, 0.0.0.0, localhost, etc.
        # -----------------------------------------------------

        result = run(
            [
                "ss",
                "-H",
                "-ltn",
            ],
            check=False,
            timeout=15,
        )

        if result.get("success"):

            for line in (
                result.get("output")
                or ""
            ).splitlines():

                parts = line.split()

                if len(parts) < 4:
                    continue

                local_address = (
                    parts[3]
                )

                match = re.search(
                    r":(\d+)$",
                    local_address,
                )

                if not match:
                    continue

                port = int(
                    match.group(1)
                )

                if (
                    start_port
                    <= port
                    <= end_port
                ):
                    ports.add(port)

            return ports

        # -----------------------------------------------------
        # FALLBACK
        #
        # Si por alguna razón `ss` no existe.
        # -----------------------------------------------------

        for port in range(
            start_port,
            end_port + 1,
        ):
            if not port_available(port):
                ports.add(port)

        return ports

    def discover_databases(
        self,
        services=None,
    ):
        """
        Inventario PostgreSQL de solo lectura.

        La asociación BD -> servicio es conservadora:
        1. db_name exacto
        2. dbfilter exacto ^nombre$
        3. db_user solamente si ese owner tiene UNA sola BD
           y existe UN solo servicio candidato.
        """

        if services is None:
            services = (
                self.discover()
                .get("services")
                or []
            )

        sql = """
            SELECT
                d.datname,
                pg_get_userbyid(d.datdba),
                pg_database_size(d.datname),
                pg_encoding_to_char(d.encoding),
                d.datcollate,
                d.datctype
            FROM pg_database d
            WHERE NOT d.datistemplate
              AND d.datallowconn
              AND d.datname <> 'postgres'
            ORDER BY d.datname;
        """

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
            check=False,
            timeout=120,
        )

        if not result.get("success"):
            return {
                "success": False,
                "message": (
                    "No fue posible consultar "
                    "las bases PostgreSQL."
                ),
                "databases": [],
                "count": 0,
                "error": (
                    result.get("output")
                    or ""
                ),
            }

        raw_databases = []

        for line in (
            result.get("output")
            or ""
        ).splitlines():

            if not line.strip():
                continue

            parts = line.split(
                "\t"
            )

            while len(parts) < 6:
                parts.append("")

            try:
                size_bytes = int(
                    parts[2]
                    or 0
                )
            except ValueError:
                size_bytes = 0

            raw_databases.append(
                {
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
                }
            )

        owner_db_count = Counter(
            database["owner"]
            for database in raw_databases
            if database.get("owner")
        )

        exact_map = defaultdict(
            list
        )

        owner_services = defaultdict(
            list
        )

        for service in services:
            db_name = str(
                service.get("db_name")
                or ""
            ).strip()

            if db_name:
                exact_map[
                    db_name
                ].append(
                    service
                )

            dbfilter = str(
                service.get("dbfilter")
                or ""
            ).strip()

            exact_filter = re.fullmatch(
                r"\^([A-Za-z0-9_.-]+)\$",
                dbfilter,
            )

            if exact_filter:
                exact_map[
                    exact_filter.group(1)
                ].append(
                    service
                )

            db_user = str(
                service.get("db_user")
                or ""
            ).strip()

            if db_user:
                owner_services[
                    db_user
                ].append(
                    service
                )

        databases = []

        for database in raw_databases:
            name = database["name"]
            owner = database["owner"]

            service = None

            exact_candidates = []

            for candidate in exact_map.get(
                name,
                [],
            ):
                if candidate not in exact_candidates:
                    exact_candidates.append(
                        candidate
                    )

            if len(exact_candidates) == 1:
                service = exact_candidates[0]

            elif (
                not exact_candidates
                and owner
                and owner_db_count.get(
                    owner,
                    0,
                ) == 1
            ):
                candidates = (
                    owner_services.get(
                        owner,
                        []
                    )
                )

                if len(candidates) == 1:
                    service = candidates[0]

            item = {
                **database,
                "service_unit": "",
                "service_technical_name": "",
                "odoo_version": "",
                "data_dir": "",
                "system_user": "",
            }

            if service:
                item.update(
                    {
                        "service_unit": (
                            service.get("unit")
                            or ""
                        ),
                        "service_technical_name": (
                            service.get(
                                "technical_name"
                            )
                            or ""
                        ),
                        "odoo_version": (
                            service.get(
                                "odoo_version"
                            )
                            or ""
                        ),
                        "data_dir": (
                            service.get(
                                "data_dir"
                            )
                            or ""
                        ),
                        "system_user": (
                            service.get("user")
                            or ""
                        ),
                    }
                )

            databases.append(
                item
            )

        return {
            "success": True,
            "message": (
                f"Se detectaron "
                f"{len(databases)} "
                "bases PostgreSQL."
            ),
            "databases": databases,
            "count": len(databases),
        }