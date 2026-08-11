import re
import socket
import subprocess


SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.@-]+$")
SAFE_OWNER = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class CommandError(RuntimeError):
    pass


def run(cmd, timeout=30, check=True, env=None):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Timeout ejecutando: {' '.join(cmd)}") from exc

    output = (result.stdout or "").strip()
    if check and result.returncode != 0:
        raise CommandError(
            f"Comando falló ({result.returncode}): {' '.join(cmd)}\n{output}"
        )
    return {
        "success": result.returncode == 0,
        "returncode": result.returncode,
        "output": output,
        "cmd": cmd,
    }


def assert_unit_name(value):
    if not value or not SAFE_UNIT.fullmatch(value):
        raise CommandError(f"Nombre de unidad systemd inválido: {value!r}")
    return value


def assert_owner(value):
    if not value or not SAFE_OWNER.fullmatch(value):
        raise CommandError(
            "Owner inválido. Use minúsculas, números y guiones; "
            "debe iniciar con una letra."
        )
    return value


def port_available(port):
    port = int(port)
    if port < 1 or port > 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def systemd_exists(unit):
    unit = assert_unit_name(unit)
    result = run(
        ["systemctl", "show", unit, "--property=LoadState", "--value"],
        check=False,
    )
    return result["output"].strip() == "loaded"


def systemd_status(unit):
    unit = assert_unit_name(unit)
    result = run(
        [
            "systemctl",
            "show",
            unit,
            "--no-page",
            "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus",
        ],
        check=False,
    )
    values = {}
    for line in result["output"].splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "unit": unit,
        "exists": values.get("LoadState") == "loaded",
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "main_pid": int(values.get("MainPID") or 0),
        "exec_main_status": int(values.get("ExecMainStatus") or 0),
        "raw": values,
    }
