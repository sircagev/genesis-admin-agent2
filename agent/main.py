import logging
import signal
import time
import traceback

from . import __version__
from .client import ControllerClient
from .config import AgentConfig
from .executor import JobExecutor
from .inventory import collect_inventory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_logger = logging.getLogger("genesis-agent")

STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


def ensure_enrolled(config, client):
    if config.get("agent_id") and config.get("agent_token"):
        return

    token = config.get("enrollment_token")
    code = config.get("server_code")
    if not token or not code:
        raise RuntimeError(
            "Falta enrollment_token o server_code en config/config.yaml."
        )

    inventory = collect_inventory(__version__)
    result = client.enroll(inventory)
    if not result.get("success"):
        raise RuntimeError(result.get("message") or "Falló enrolamiento.")

    config.save_identity(result["agent_id"], result["agent_token"])
    client.config.reload()
    _logger.info("Agente registrado para servidor %s", code)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config = AgentConfig()
    client = ControllerClient(config)
    ensure_enrolled(config, client)
    executor = JobExecutor(config)

    poll_interval = max(int(config.get("poll_interval") or 3), 1)
    heartbeat_interval = max(int(config.get("heartbeat_interval") or 30), 10)
    next_heartbeat = 0.0

    _logger.info("Genesis Infrastructure Agent %s iniciado", __version__)

    while not STOP:
        now = time.monotonic()

        try:
            if now >= next_heartbeat:
                client.heartbeat(collect_inventory(__version__))
                next_heartbeat = now + heartbeat_interval

            response = client.next_job()
            job = response.get("job")
            if not job:
                time.sleep(poll_interval)
                continue

            job_id = job["id"]
            _logger.info("Trabajo %s: %s", job_id, job.get("job_type"))
            client.job_started(job_id)

            try:
                result = executor.execute(job)
                success = bool(result.get("success", True))
                if success:
                    client.job_result(job_id, True, result=result)
                else:
                    client.job_result(
                        job_id,
                        False,
                        result=result,
                        error=result.get("message") or "Operación fallida",
                    )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Trabajo %s falló", job_id)
                client.job_result(
                    job_id,
                    False,
                    error=f"{exc}\n{traceback.format_exc(limit=8)}",
                )

        except Exception:  # pylint: disable=broad-except
            _logger.exception("Error de comunicación con el controlador")
            time.sleep(min(poll_interval * 2, 30))

    _logger.info("Genesis Infrastructure Agent detenido")


if __name__ == "__main__":
    main()
