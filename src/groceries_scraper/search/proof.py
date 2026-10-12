"""A fixture-only search v1 origin that proves the private route before any real search exists."""

import hmac
import json
import select
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

CONTRACT_VERSION = 1
ADAPTER_VERSION = "fixture-v1"
RETAILER = "fixture"
MAX_BODY = 4096
MAX_BUDGET_MS = 10_000
MAX_ADMISSION_BUDGET_MS = 2_000
# The query names a fixture stream, so the proof needs no retailer and no redeploy per scenario.
SCENARIOS = frozenset(
    {"ok", "fragmented", "oversize", "mismatched", "duplicate", "premature", "late"}
)
MAX_TERMINAL_FRAME = 64 * 1024
FRAGMENT = 7  # bytes: splits every frame mid-token
LATE_BY = 0.25  # seconds past the admission budget


def _bounded_ms(value: object, maximum: int) -> bool:
    return type(value) is int and 0 < value <= maximum


def _validated(body: bytes) -> dict[str, Any] | None:
    try:
        request = json.loads(body)
    except ValueError:
        return None
    if not isinstance(request, dict):
        return None
    try:
        expires_at = datetime.fromisoformat(request["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    valid = (
        request.get("contract_version") == CONTRACT_VERSION
        and isinstance(request.get("request_id"), str)
        and 0 < len(request["request_id"]) <= 128
        and isinstance(request.get("execution_key"), str)
        and request.get("retailer") == RETAILER
        and request.get("adapter_version") == ADAPTER_VERSION
        and request.get("query") in SCENARIOS
        and isinstance(request.get("context"), dict)
        and _bounded_ms(request.get("budget_ms"), MAX_BUDGET_MS)
        and _bounded_ms(request.get("admission_budget_ms"), MAX_ADMISSION_BUDGET_MS)
        and expires_at.tzinfo is not None
        and expires_at > datetime.now(UTC)
    )
    return request if valid else None


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class _CallerGone(Exception):
    pass


class ProofResponder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], *, token_file: Path, terminal_delay: float
    ) -> None:
        super().__init__(address, _Handler)
        self.token_file = token_file
        self.terminal_delay = terminal_delay
        # One fresh execution at a time, as the real service's global slot will allow.
        self.slot = Lock()


class _Handler(BaseHTTPRequestHandler):
    server: ProofResponder
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.close_connection = True
        if self.path != "/healthz":
            return self._reject(404, "not_found")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def do_POST(self) -> None:
        self.close_connection = True
        if self.path != "/v1/search":
            return self._reject(404, "not_found")
        if not self._authorized():
            return self._reject(401, "unauthorized")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return self._reject(400, "invalid_request")
        if length > MAX_BODY:
            return self._reject(413, "too_large")
        request = _validated(self.rfile.read(length)) if length >= 0 else None
        if request is None:
            return self._reject(400, "invalid_request")
        if not self.server.slot.acquire(blocking=False):
            return self._reject(409, "busy")
        try:
            try:
                self._stream(request)
            finally:
                # Freed before EOF, so a caller that saw the end may search again at once.
                self.server.slot.release()
            self._chunk(b"")
        except (BrokenPipeError, ConnectionResetError, _CallerGone):
            # Nothing replays: an abandoned execution just ends.
            self.log_message("caller disconnected from %s", request["request_id"])

    def _stream(self, request: dict[str, Any]) -> None:
        started = time.monotonic()
        budget = request["budget_ms"] / 1000
        scenario = request["query"]
        identity = {
            "contract_version": CONTRACT_VERSION,
            "request_id": request["request_id"],
            "execution_id": str(uuid.uuid4()),
        }
        accepted = {
            **identity,
            "type": "accepted",
            "adapter_version": ADAPTER_VERSION,
            "execution_expires_at": _timestamp(datetime.now(UTC) + timedelta(seconds=budget)),
            "joined": False,
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        if scenario == "late":
            self._wait(request["admission_budget_ms"] / 1000 + LATE_BY)
        self._frame(accepted, fragmented=scenario == "fragmented")
        if scenario == "duplicate":
            self._frame(accepted)

        remaining = budget - (time.monotonic() - started)
        if self.server.terminal_delay >= remaining:
            self._wait(max(remaining, 0))
            terminal = {**identity, "type": "error", "code": "deadline_exceeded"}
        else:
            self._wait(self.server.terminal_delay)
            terminal = {
                **identity,
                "type": "result",
                "status": "ok",
                "source_at": _timestamp(datetime.now(UTC)),
                "results": [],
            }
            if scenario == "oversize":
                terminal["padding"] = "x" * MAX_TERMINAL_FRAME
            elif scenario == "mismatched":
                terminal["execution_id"] = str(uuid.uuid4())
        if scenario != "premature":
            self._frame(terminal, fragmented=scenario == "fragmented")

    def _wait(self, seconds: float) -> None:
        """Sleep, but notice at once if the caller hangs up: it has nothing more to send."""
        readable, _, _ = select.select([self.connection], [], [], seconds)
        if readable and not self.connection.recv(1, socket.MSG_PEEK):
            raise _CallerGone

    def _authorized(self) -> bool:
        """Re-read per request so a rotated Secret applies without a restart."""
        try:
            expected = self.server.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return False  # fail closed rather than serve an unconfigured origin
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(
            supplied.encode(), f"Bearer {expected}".encode()
        )

    def _reject(self, status: int, code: str) -> None:
        """A terminal JSON rejection: no acceptance was sent and nothing executes."""
        body = json.dumps(
            {"contract_version": CONTRACT_VERSION, "type": "rejected", "code": code}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _frame(self, frame: dict[str, Any], *, fragmented: bool = False) -> None:
        line = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        if not fragmented:
            return self._chunk(line)
        # Small pauses keep the pieces apart on the wire, not coalesced into one read.
        for start in range(0, len(line), FRAGMENT):
            self._chunk(line[start : start + FRAGMENT])
            self._wait(0.01)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()
