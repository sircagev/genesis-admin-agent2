import requests


class ControllerClient:
    def __init__(self, config):
        self.config = config
        self.base = str(config.get("controller_url") or "").rstrip("/")
        self.verify = bool(config.get("verify_tls", True))
        self.timeout = int(config.get("request_timeout") or 30)
        self.session = requests.Session()

    def _url(self, path):
        database = str(self.config.get("database") or "").strip()
        url = f"{self.base}{path}"
        if database:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}db={database}"
        return url

    def _identity_headers(self):
        agent_id = self.config.get("agent_id")
        agent_token = self.config.get("agent_token")
        return {
            "X-Agent-ID": agent_id or "",
            "X-Agent-Token": agent_token or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path, payload, identity=True):
        headers = self._identity_headers() if identity else {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = self.session.post(
            self._url(path),
            json=payload,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
        )
        response.raise_for_status()
        return response.json()

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
        return self._post("/infra/agent/v1/heartbeat", {"inventory": inventory})

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
