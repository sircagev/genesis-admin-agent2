import logging
import signal
import time
import traceback

import threading

from . import __version__
from .client import ControllerClient
from .config import AgentConfig
from .executor import JobExecutor
from .inventory import collect_inventory
from .discovery import OdooServiceDiscovery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_logger = logging.getLogger("genesis-agent")

STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


def try_enroll(config, client):
    if config.is_enrolled():
        return True

    token = str(config.get("enrollment_token") or "").strip()
    code = str(config.get("server_code") or "").strip()

    if not token:
        _logger.error(
            "Agente no enrolado. Genere un token en Odoo y ejecute "
            "`sudo genesis-agent reenroll TOKEN`."
        )
        return False

    try:
        result = client.enroll(collect_inventory(__version__))
    except Exception as exc:  # pylint: disable=broad-except
        _logger.error("No fue posible enrolar el agente: %s", exc)
        return False

    if not result.get("success"):
        _logger.error(
            "Falló enrolamiento: %s",
            result.get("message") or "respuesta desconocida",
        )
        return False

    config.save_identity(result["agent_id"], result["agent_token"])
    config.reload()
    _logger.info("Agente registrado para servidor %s", code)
    return True


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config = AgentConfig()
    client = ControllerClient(config)

    _logger.info("Genesis Infrastructure Agent %s iniciado", __version__)

    # Do not crash-loop forever when an enrollment token is stale.
    while not STOP and not config.is_enrolled():
        if try_enroll(config, client):
            break
        time.sleep(30)
        config.reload()
        client = ControllerClient(config)

    if STOP:
        return

    executor = JobExecutor(config)
    discovery = OdooServiceDiscovery(config)
    poll_interval = max(int(config.get("poll_interval") or 3), 1)
    heartbeat_interval = max(int(config.get("heartbeat_interval") or 30), 10)
    next_heartbeat = 0.0
    service_sync_interval = max(int(config.get("service_sync_interval") or 300), 60,)
    next_service_sync = 0.0

    while not STOP:
        now = time.monotonic()

        try:
            if now >= next_heartbeat:
                client.heartbeat(collect_inventory(__version__))
                next_heartbeat = now + heartbeat_interval

            if now >= next_service_sync:
                try:
                    discovered = discovery.discover()

                    result = client.sync_services(
                        discovered.get("services") or []
                    )

                    _logger.info(
                        "Sincronización automática Odoo: %s",
                        result.get("message") or result,
                    )

                except Exception:
                    _logger.exception(
                        "No fue posible sincronizar automáticamente "
                        "los servicios Odoo"
                    )

                finally:
                    next_service_sync = (
                        now + service_sync_interval
                    )

                try:
                    database_inventory = (
                        discovery
                        .discover_databases(
                            discovered.get(
                                "services"
                            )
                            or []
                        )
                    )

                    if database_inventory.get(
                        "success"
                    ):
                        database_result = (
                            client
                            .sync_databases(
                                database_inventory.get(
                                    "databases"
                                )
                                or []
                            )
                        )

                        _logger.info(
                            "Sincronización automática "
                            "PostgreSQL: %s",
                            (
                                database_result.get(
                                    "message"
                                )
                                or database_result
                            ),
                        )

                    else:
                        _logger.warning(
                            "Inventario PostgreSQL: %s",
                            database_inventory.get(
                                "message"
                            ),
                        )

                except Exception:
                    _logger.exception(
                        "No fue posible sincronizar "
                        "automáticamente las bases "
                        "PostgreSQL"
                    )

            response = client.next_job()
            job = response.get("job")
            if not job:
                time.sleep(poll_interval)
                continue

            job_id = job["id"]
            _logger.info("Trabajo %s: %s", job_id, job.get("job_type"))
            client.job_started(job_id)

            progress_lock = threading.Lock()

            progress_state = {
                "stage": "running",
                "percent": 1,
                "message": "Trabajo iniciado.",
            }

            keepalive_stop = threading.Event()

            def report_progress(
                stage,
                percent,
                message,
            ):
                with progress_lock:
                    progress_state["stage"] = str(
                        stage or "running"
                    )

                    progress_state["percent"] = int(
                        percent or 0
                    )

                    progress_state["message"] = str(
                        message or ""
                    )

                try:
                    client.job_progress(
                        job_id,
                        progress_state["stage"],
                        progress_state["percent"],
                        progress_state["message"],
                    )

                except Exception:
                    _logger.warning(
                        "No fue posible reportar progreso del trabajo %s",
                        job_id,
                        exc_info=True,
                    )

            def progress_keepalive():
                while not keepalive_stop.wait(20):
                    with progress_lock:
                        stage = progress_state["stage"]
                        percent = progress_state["percent"]
                        message = progress_state["message"]

                    try:
                        client.job_progress(
                            job_id,
                            stage,
                            percent,
                            message,
                        )

                    except Exception:
                        _logger.warning(
                            "No fue posible enviar keepalive "
                            "del trabajo %s",
                            job_id,
                            exc_info=True,
                        )

            executor.set_progress_callback(
                report_progress
            )

            keepalive_thread = threading.Thread(
                target=progress_keepalive,
                name=f"job-{job_id}-keepalive",
                daemon=True,
            )

            keepalive_thread.start()

            try:
                result = executor.execute(job)
                success = bool(result.get("success", True))
                client.job_result(
                    job_id,
                    success,
                    result=result,
                    error=(
                        ""
                        if success
                        else result.get("message") or "Operación fallida"
                    ),
                )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Trabajo %s falló", job_id)
                client.job_result(
                    job_id,
                    False,
                    error=f"{exc}\n{traceback.format_exc(limit=8)}",
                )
            
            finally:
                keepalive_stop.set()

                keepalive_thread.join(
                    timeout=2
                )

                executor.set_progress_callback(
                    None
                )

        except Exception:  # pylint: disable=broad-except
            _logger.exception("Error de comunicación con el controlador")
            time.sleep(min(poll_interval * 2, 30))

    _logger.info("Genesis Infrastructure Agent detenido")


if __name__ == "__main__":
    main()
