"""The few Kubernetes API calls the Dispatcher's ServiceAccount is allowed to make."""

import json
import os
import ssl
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from groceries_scraper.dispatch.dispatcher import JobStatus

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubernetesCluster:
    def __init__(
        self,
        api_url: str,
        token: str,
        namespace: str,
        context: ssl.SSLContext | None = None,
        timeout: float = 30,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.namespace = namespace
        self._token = token
        self._context = context
        self._timeout = timeout

    @classmethod
    def in_cluster(cls) -> Self:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        return cls(
            f"https://{host}:{port}",
            (SERVICE_ACCOUNT / "token").read_text().strip(),
            (SERVICE_ACCOUNT / "namespace").read_text().strip(),
            ssl.create_default_context(cafile=SERVICE_ACCOUNT / "ca.crt"),
        )

    def create_job(self, job: dict[str, Any]) -> str:
        created = self._request("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", job)
        uid: str = created["metadata"]["uid"]
        return uid

    def create_config_map(self, config_map: dict[str, Any]) -> None:
        self._request("POST", f"/api/v1/namespaces/{self.namespace}/configmaps", config_map)

    def job_status(self, name: str) -> JobStatus:
        try:
            job = self._request("GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}")
        except HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return "missing"
            raise
        conditions = job.get("status", {}).get("conditions") or []
        ended = any(
            c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
            for c in conditions
        )
        return "finished" if ended else "running"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if body is not None:
            # The ServiceAccount may act only in its own namespace.
            body = {**body, "metadata": {**body["metadata"], "namespace": self.namespace}}
        request = Request(
            self.api_url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=self._timeout, context=self._context) as response:
            return json.load(response)
