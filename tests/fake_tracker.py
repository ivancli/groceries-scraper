"""A Collector API served on localhost, answering like the tracker."""

import hashlib
import json
import socket
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest


class FakeTracker:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.responses: list[tuple[int, dict[str, str], Any]] = []
        self.reject: dict[str, str] = {}
        self.url = ""
        # Answers watch-list GETs that have no queued response.
        self.watch_list: list[dict[str, Any]] | None = None


def serve_tracker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTracker]:
    tracker = FakeTracker()
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "test-client-secret")

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            data = json.loads(body) if body else None
            tracker.requests.append((self.command, self.path, dict(self.headers), data))
            if tracker.responses:
                status, headers, response = tracker.responses.pop(0)
            elif self.path.endswith("/watch-list") and tracker.watch_list is not None:
                version = hashlib.sha256(json.dumps(tracker.watch_list).encode()).hexdigest()
                if self.headers.get("If-None-Match") == f'"{version}"':
                    status, headers, response = 304, {}, None
                else:
                    status, headers = 200, {"ETag": f'"{version}"'}
                    response = {"version": version, "entries": tracker.watch_list}
            elif self.command == "PUT":
                status, headers, response = 200, {}, {}
            else:
                status, headers = 200, {}
                items = data.get("items", []) if data else []
                response = {
                    "acked": [item["id"] for item in items if item["id"] not in tracker.reject],
                    "rejected": [
                        {"id": item["id"], "reason": tracker.reject[item["id"]]}
                        for item in items
                        if item["id"] in tracker.reject
                    ],
                }
            if status == 0:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if status != 304:
                self.wfile.write(json.dumps(response).encode())

        do_POST = handle_request
        do_GET = handle_request
        do_PUT = handle_request

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tracker.url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tracker
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
