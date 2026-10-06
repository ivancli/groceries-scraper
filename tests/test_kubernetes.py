import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from groceries_scraper.dispatch.kubernetes import KubernetesCluster


class FakeApiServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.url = ""


@pytest.fixture
def api() -> Iterator[FakeApiServer]:
    api = FakeApiServer()

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            data: Any = json.loads(body) if body else None
            api.requests.append((self.command, self.path, self.headers["Authorization"], data))
            name = self.path.rsplit("/", 1)[-1]
            if self.command == "POST":
                status, response = 201, {**data, "metadata": {**data["metadata"], "uid": "u-1"}}
                if self.path.endswith("/jobs"):
                    api.jobs[data["metadata"]["name"]] = response
            elif name in api.jobs:
                status, response = 200, api.jobs[name]
            else:
                status, response = 404, {"kind": "Status", "code": 404}
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api.url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_jobs_and_config_maps_are_created_in_the_service_accounts_namespace(
    api: FakeApiServer,
) -> None:
    cluster = KubernetesCluster(api.url, "sa-token", "groceries")
    job = {"kind": "Job", "metadata": {"name": "scrape-a-x", "namespace": "other"}}

    assert cluster.create_job(job) == "u-1"
    cluster.create_config_map({"kind": "ConfigMap", "metadata": {"name": "scrape-a-x"}})

    assert [(method, path, auth) for method, path, auth, _ in api.requests] == [
        ("POST", "/apis/batch/v1/namespaces/groceries/jobs", "Bearer sa-token"),
        ("POST", "/api/v1/namespaces/groceries/configmaps", "Bearer sa-token"),
    ]
    assert {data["metadata"]["namespace"] for *_, data in api.requests} == {"groceries"}


def test_a_job_is_running_until_complete_or_failed_and_missing_once_deleted(
    api: FakeApiServer,
) -> None:
    cluster = KubernetesCluster(api.url, "sa-token", "groceries")
    cluster.create_job({"kind": "Job", "metadata": {"name": "scrape-a-x"}})

    assert cluster.job_status("scrape-a-x") == "running"
    api.jobs["scrape-a-x"]["status"] = {"conditions": [{"type": "FailureTarget", "status": "True"}]}
    assert cluster.job_status("scrape-a-x") == "running"
    api.jobs["scrape-a-x"]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    assert cluster.job_status("scrape-a-x") == "finished"
    assert cluster.job_status("scrape-gone") == "missing"
