import requests


class ControllerClient:
    def __init__(self, config):
        self.config = config
        self.base = str(config.get("controller_url") or "").rstrip("/")
        self.verify = bool(config.get("verify_tls", True))
        self.timeout = int(config.get("request_timeout") or 30)
        self.session = requests.Session()

    def _url(self, path):
        return f"{self.base}{path}"

    def _base_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        database = str(self.config.get("database") or "").strip()
        if database:
            headers["X-Odoo-Database"] = database
        return headers

    def _identity_headers(self):
        headers = self._base_headers()
        headers.update(
            {
                "X-Agent-ID": self.config.get("agent_id") or "",
                "X-Agent-Token": self.config.get("agent_token") or "",
            }
        )
        return headers

    def _post(self, path, payload, identity=True):
        headers = (
            self._identity_headers()
            if identity
            else self._base_headers()
        )

        response = self.session.post(
            self._url(path),
            json=payload,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
        )

        if not response.ok:
            body = (response.text or "").strip()
            raise RuntimeError(
                f"HTTP {response.status_code} llamando {self._url(path)}: "
                f"{body[:1200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"El controlador no devolvió JSON en {self._url(path)}."
            ) from exc

    def enroll(self, inventory):
        return self._post(
            "/infra/agent/v1/enroll",
            {
                "server_code": self.config.get("server_code"),
                "enrollment_token": self.config.get("enrollment_token"),
                "inventory": inventory,
            },
            identity=False,
        )

    def heartbeat(self, inventory):
        return self._post(
            "/infra/agent/v1/heartbeat",
            {"inventory": inventory},
        )

    def next_job(self):
        return self._post("/infra/agent/v1/jobs/next", {})

    def job_started(self, job_id):
        return self._post(
            f"/infra/agent/v1/jobs/{job_id}/started",
            {},
        )

    def job_result(self, job_id, success, result=None, error=None):
        return self._post(
            f"/infra/agent/v1/jobs/{job_id}/result",
            {
                "success": bool(success),
                "result": result or {},
                "error": error or "",
            },
        )
