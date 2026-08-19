import os
import platform
import shutil
import socket
from pathlib import Path

from .bootstrap import collect_bootstrap_inventory


def _machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    return socket.gethostname()


def _memory():
    result = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            value = raw.strip().split()[0]
            result[key] = int(value)
    except OSError:
        pass
    return {
        "total_mb": result.get("MemTotal", 0) // 1024,
        "available_mb": result.get("MemAvailable", 0) // 1024,
    }


def _load_average():
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": one, "5m": five, "15m": fifteen}
    except OSError:
        return {}


def _addresses():
    values = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            addr = item[4][0]
            if addr and not addr.startswith("127.") and addr != "::1":
                values.add(addr)
    except socket.gaierror:
        pass
    return sorted(values)


def collect_inventory(agent_version):
    disk = shutil.disk_usage("/")
    return {
        "agent_version": agent_version,
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "machine_id": _machine_id(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
        "memory": _memory(),
        "disk_root": {
            "total_mb": disk.total // (1024 * 1024),
            "used_mb": disk.used // (1024 * 1024),
            "free_mb": disk.free // (1024 * 1024),
        },
        "load_average": _load_average(),
        "addresses": _addresses(),
        "bootstrap": collect_bootstrap_inventory(),
    }
