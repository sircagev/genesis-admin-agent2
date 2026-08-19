from pathlib import Path

import requests


class ControllerClient:
    def __init__(self, config):
        self.config = config

        self.base = str(
            config.get("controller_url")
            or ""
        ).rstrip("/")

        self.verify = bool(
            config.get(
                "verify_tls",
                True,
            )
        )

        self.timeout = int(
            config.get(
                "request_timeout"
            )
            or 30
        )

        self.session = requests.Session()

    def _url(self, path):
        return f"{self.base}{path}"

    def _base_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        database = str(
            self.config.get("database")
            or ""
        ).strip()

        if database:
            headers["X-Odoo-Database"] = database

        return headers

    def _identity_headers(self):
        headers = self._base_headers()

        headers.update(
            {
                "X-Agent-ID": (
                    self.config.get("agent_id")
                    or ""
                ),
                "X-Agent-Token": (
                    self.config.get("agent_token")
                    or ""
                ),
            }
        )

        return headers

    @staticmethod
    def _raise_for_status(response, url):
        if response.ok:
            return

        body = (
            response.text
            or ""
        ).strip()

        raise RuntimeError(
            f"HTTP {response.status_code} "
            f"llamando {url}: "
            f"{body[:1200]}"
        )

    def _post(
        self,
        path,
        payload,
        identity=True,
    ):
        headers = (
            self._identity_headers()
            if identity
            else self._base_headers()
        )

        url = self._url(path)

        response = self.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
        )

        self._raise_for_status(
            response,
            url,
        )

        try:
            return response.json()

        except ValueError as exc:
            raise RuntimeError(
                f"El controlador no devolvió JSON en {url}."
            ) from exc

    def enroll(self, inventory):
        return self._post(
            "/infra/agent/v1/enroll",
            {
                "server_code": (
                    self.config.get("server_code")
                ),
                "enrollment_token": (
                    self.config.get("enrollment_token")
                ),
                "inventory": inventory,
            },
            identity=False,
        )

    def sync_services(self, services):
        return self._post(
            "/infra/agent/v1/services/sync",
            {
                "services": services,
            },
        )

    def sync_databases(self, databases):
        return self._post(
            "/infra/agent/v1/databases/sync",
            {
                "databases": databases,
            },
        )

    def heartbeat(self, inventory):
        return self._post(
            "/infra/agent/v1/heartbeat",
            {
                "inventory": inventory,
            },
        )

    def provisioning_config(self):
        return self._post(
            "/infra/agent/v1/config/provisioning",
            {},
        )

    def next_job(self):
        return self._post(
            "/infra/agent/v1/jobs/next",
            {},
        )

    def job_started(self, job_id):
        return self._post(
            f"/infra/agent/v1/jobs/{job_id}/started",
            {},
        )

    def job_progress(
        self,
        job_id,
        stage,
        percent,
        message="",
    ):
        return self._post(
            f"/infra/agent/v1/jobs/{job_id}/progress",
            {
                "stage": str(
                    stage
                    or ""
                ),
                "percent": int(
                    percent
                    or 0
                ),
                "message": str(
                    message
                    or ""
                ),
            },
        )

    def job_result(
        self,
        job_id,
        success,
        result=None,
        error=None,
    ):
        return self._post(
            f"/infra/agent/v1/jobs/{job_id}/result",
            {
                "success": bool(success),
                "result": result or {},
                "error": error or "",
            },
        )

    # =========================================================
    # TRANSFERENCIA DE BASES DE DATOS
    # =========================================================

    def transfer_begin(
        self,
        token,
        file_name,
        size,
        sha256,
    ):
        return self._post(
            (
                "/infra/agent/v1/"
                f"database-transfers/{token}/begin"
            ),
            {
                "file_name": file_name,
                "size": int(size),
                "sha256": sha256,
            },
        )

    def transfer_upload(
        self,
        token,
        offset,
        data,
    ):
        headers = self._identity_headers()

        headers["Content-Type"] = (
            "application/octet-stream"
        )

        headers["Accept"] = (
            "application/json"
        )

        url = self._url(
            (
                "/infra/agent/v1/"
                f"database-transfers/{token}/"
                f"upload/{int(offset)}"
            )
        )

        response = self.session.post(
            url,
            data=data,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
        )

        self._raise_for_status(
            response,
            url,
        )

        try:
            return response.json()

        except ValueError as exc:
            raise RuntimeError(
                "Respuesta inválida durante upload."
            ) from exc

    def transfer_complete(
        self,
        token,
        size,
        sha256,
    ):
        return self._post(
            (
                "/infra/agent/v1/"
                f"database-transfers/{token}/complete"
            ),
            {
                "size": int(size),
                "sha256": sha256,
            },
        )

    def transfer_download(
        self,
        token,
        target_path,
    ):
        headers = self._identity_headers()

        # GET no necesita Content-Type.
        headers.pop(
            "Content-Type",
            None,
        )

        headers["Accept"] = (
            "application/octet-stream"
        )

        url = self._url(
            (
                "/infra/agent/v1/"
                f"database-transfers/{token}/download"
            )
        )

        response = self.session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(
                self.timeout,
                3600,
            ),
            verify=self.verify,
        )

        self._raise_for_status(
            response,
            url,
        )

        target = Path(target_path)

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with target.open("wb") as handle:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    handle.write(chunk)

        return {
            "success": True,
            "path": str(target),
            "size": target.stat().st_size,
        }