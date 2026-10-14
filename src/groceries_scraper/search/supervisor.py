"""One isolated crawler child per fresh search, under the shared Search Admission.

The child never holds a coordinator credential: it asks this supervisor for each Retailer
Permit over a socket bound to its admitted execution. Admission ends only once the child's
whole session (its browser included) is verified gone.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from groceries_scraper.adapter.traffic import site_policy
from groceries_scraper.coordination import (
    Admitted,
    Coordinator,
    CoordinatorUnavailable,
    Permit,
)
from groceries_scraper.search.adapters import SearchAdapter, valid_query

logger = logging.getLogger(__name__)

DEFAULT_RUNNER = (sys.executable, "-m", "groceries_scraper.search.child")
# Nothing that could reach a Sink, the Dispatcher or the coordinator.
ENV_ALLOWLIST = ("PATH", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
MAX_MESSAGE = 64 * 1024  # a terminal frame's limit
MAX_MESSAGES = 1000
DELIVERY_MARGIN = 0.25  # seconds the child's budget leaves for the result to reach the caller
REMEMBER_REQUESTS = timedelta(minutes=15)
MAX_BUDGET_MS = 10_000
MAX_ADMISSION_BUDGET_MS = 2_000
CHILD_STATUSES = frozenset({"ok", "blocked", "failed"})


@dataclass(frozen=True)
class SearchRequest:
    request_id: str
    execution_key: str
    retailer: str
    adapter_version: str
    query: str
    context: Mapping[str, Any]
    budget_ms: int
    admission_budget_ms: int
    expires_at: datetime

    @property
    def identity(self) -> tuple[str, ...]:
        """What must match for two requests to share one execution."""
        context = json.dumps(dict(self.context), sort_keys=True)
        return self.execution_key, self.retailer, self.adapter_version, self.query, context


@dataclass(frozen=True)
class Accepted:
    execution_id: str
    execution_expires_at: datetime
    joined: bool


@dataclass(frozen=True)
class Outcome:
    status: str
    execution_id: str | None = None
    joined: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Execution:
    id: str
    identity: tuple[str, ...]
    adapter: SearchAdapter
    request: SearchRequest
    budget: float
    admission: "asyncio.Future[Accepted | Outcome]"
    result: "asyncio.Future[Outcome]"
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    callers: int = 0
    refusal: Outcome | None = None
    permits: dict[int, Permit] = field(default_factory=dict)


@dataclass
class _KnownRequest:
    identity: tuple[str, ...]
    execution: _Execution
    at: datetime


class Supervisor:
    def __init__(
        self,
        coordinator: Coordinator,
        adapters: Mapping[str, SearchAdapter],
        owner: str,
        *,
        runner: Sequence[str] = DEFAULT_RUNNER,
        workdir: Path | None = None,
        grace: float = 1.0,
    ) -> None:
        self.coordinator = coordinator
        self.adapters = adapters
        self.owner = owner
        self.runner = tuple(runner)
        self.workdir = workdir
        self.grace = grace
        self._executions: dict[str, _Execution] = {}
        self._known: dict[str, _KnownRequest] = {}
        self._tasks: set[asyncio.Future[Any]] = set()

    async def search(
        self, request: SearchRequest, on_accepted: Callable[[Accepted], object] | None = None
    ) -> Outcome:
        """Cancelling the caller detaches it; the last caller to detach stops the execution."""
        if (refused := self._refuse_input(request)) is not None:
            return refused
        # A joiner shares the execution but never waits past its own deadline.
        budget = min(
            request.budget_ms / 1000, (request.expires_at - datetime.now(UTC)).total_seconds()
        )
        if budget <= 0:
            return Outcome("timeout", error="expired")
        execution = self._executions.get(request.execution_key)
        joined = execution is not None
        if execution is None:
            execution = self._start(request, budget)
        elif execution.identity != request.identity:
            return Outcome("invalid_request", error="conflicting_input")
        self._known[request.request_id] = _KnownRequest(
            request.identity, execution, datetime.now(UTC)
        )
        execution.callers += 1
        try:
            async with asyncio.timeout(budget):
                accepted = await asyncio.shield(execution.admission)
                if isinstance(accepted, Outcome):
                    return accepted
                if on_accepted is not None:
                    on_accepted(replace(accepted, joined=joined))
                outcome = await asyncio.shield(execution.result)
            return replace(outcome, joined=joined)
        except TimeoutError:
            return Outcome("timeout", execution.id, joined=joined)
        finally:
            execution.callers -= 1
            if execution.callers == 0 and not execution.result.done():
                self._stop_execution(execution)

    async def idle(self) -> None:
        """Until every execution has been cleaned up."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _stop_execution(self, execution: _Execution) -> None:
        execution.stop.set()
        # Stopping, it can't be joined: the same work starts afresh, Busy until cleanup ends.
        if self._executions.get(execution.request.execution_key) is execution:
            del self._executions[execution.request.execution_key]

    def _spawn(self, awaitable: Any) -> None:
        """Kept until done, so cleanup outlives the caller that started it."""
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _forget_expired_requests(self) -> None:
        now = datetime.now(UTC)
        for request_id, known in list(self._known.items()):
            if now - known.at > REMEMBER_REQUESTS:
                del self._known[request_id]

    def _refuse_input(self, request: SearchRequest) -> Outcome | None:
        adapter = self.adapters.get(request.retailer)
        if adapter is None or adapter.spec.adapter_version != request.adapter_version:
            return Outcome("unsupported_retailer")
        if (
            not valid_query(request.query)
            or request.context.get("location") not in adapter.locations
            or not 0 < request.budget_ms <= MAX_BUDGET_MS
            or not 0 < request.admission_budget_ms <= MAX_ADMISSION_BUDGET_MS
        ):
            return Outcome("invalid_request", error="invalid_input")
        self._forget_expired_requests()
        if (known := self._known.get(request.request_id)) is not None:
            if known.execution.result.done():
                return Outcome("invalid_request", error="request_reused")
            if known.identity != request.identity:
                return Outcome("invalid_request", error="conflicting_input")
        return None

    def _start(self, request: SearchRequest, budget: float) -> _Execution:
        loop = asyncio.get_running_loop()
        execution = _Execution(
            request.request_id,
            request.identity,
            self.adapters[request.retailer],
            request,
            budget,
            loop.create_future(),
            loop.create_future(),
        )
        self._executions[request.execution_key] = execution
        self._spawn(self._run(execution, time.monotonic() + budget))
        return execution

    async def _run(self, execution: _Execution, deadline: float) -> None:
        try:
            admitted = await self._admit(execution)
            if isinstance(admitted, Outcome):
                execution.admission.set_result(admitted)
                return
            execution.admission.set_result(
                Accepted(execution.id, admitted.deadline_at, joined=False)
            )
            if execution.stop.is_set():  # every caller left during admission: run nothing
                await asyncio.to_thread(self._end, admitted)
                return
            await self._execute(execution, admitted, deadline)
        finally:
            if self._executions.get(execution.request.execution_key) is execution:
                del self._executions[execution.request.execution_key]
            for future in (execution.admission, execution.result):
                if not future.done():
                    future.set_result(Outcome("failed", execution.id, error="supervisor"))

    async def _admit(self, execution: _Execution) -> Admitted | Outcome:
        request = execution.request
        expires_at = datetime.now(UTC) + timedelta(seconds=execution.budget)
        admission = asyncio.ensure_future(
            asyncio.to_thread(
                self.coordinator.admit_search,
                execution.id,
                request.execution_key,
                self.owner,
                expires_at,
            )
        )
        budget = min(request.admission_budget_ms / 1000, execution.budget)
        try:
            result = await asyncio.wait_for(asyncio.shield(admission), budget)
        except TimeoutError:
            # An admission that commits after the caller gave up ran nothing: end it.
            admission.add_done_callback(self._end_unused)
            return Outcome("timeout", error="admission")
        except CoordinatorUnavailable:
            return Outcome("unavailable")
        if result == "busy":
            return Outcome("busy")
        if result == "replayed":
            return Outcome("invalid_request", error="request_reused")
        if result == "expired":
            return Outcome("timeout", error="expired")
        return result

    def _end_unused(self, admission: "asyncio.Future[Any]") -> None:
        if admission.cancelled() or admission.exception() is not None:
            return
        if isinstance(admitted := admission.result(), Admitted):
            self._spawn(asyncio.to_thread(self._end, admitted))

    async def _execute(self, execution: _Execution, admitted: Admitted, deadline: float) -> None:
        if deadline - time.monotonic() <= DELIVERY_MARGIN:
            await asyncio.to_thread(self._end, admitted)
            execution.result.set_result(Outcome("timeout", execution.id))
            return
        base = Path(tempfile.mkdtemp(prefix="search-", dir=self.workdir))
        ours, theirs = socket.socketpair()
        env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
        env |= {"HOME": str(base), "TMPDIR": str(base)}
        # Installed browsers stay where they are; the child's own HOME holds no profile.
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
        process = None
        outcome = Outcome("failed", execution.id, error="supervisor")
        try:
            with (base / "child.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    *self.runner,
                    str(theirs.fileno()),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=base,
                    env=env,
                    start_new_session=True,  # its whole tree is one session to stop
                    pass_fds=(theirs.fileno(),),
                )
            theirs.close()
            outcome = await self._converse(execution, admitted, ours, deadline)
        finally:
            theirs.close()
            ours.close()
            verified = process is None or await self._stop(process)
            if verified:
                await asyncio.to_thread(self._end, admitted)
            else:
                logger.error(
                    "Search %s left processes behind; admission stays held for the operator",
                    execution.id,
                )
            if outcome.status not in ("ok", "cancelled"):
                _log_tail(execution.id, outcome, base / "child.log")
            shutil.rmtree(base, ignore_errors=True)
        if not execution.result.done():
            execution.result.set_result(outcome)

    def _end(self, admitted: Admitted) -> None:
        try:
            if not self.coordinator.end_search(admitted):
                logger.warning("Search %s had already been recovered", admitted.execution_id)
        except CoordinatorUnavailable:
            logger.exception("Search %s could not release admission", admitted.execution_id)

    async def _converse(
        self, execution: _Execution, admitted: Admitted, ours: socket.socket, deadline: float
    ) -> Outcome:
        reader, writer = await asyncio.open_connection(sock=ours, limit=MAX_MESSAGE)
        try:
            remaining = deadline - time.monotonic() - DELIVERY_MARGIN
            job = {
                "type": "job",
                "adapter": str(execution.adapter.source),
                "query": execution.request.query,
                "location": execution.request.context["location"],
                "budget": remaining,
            }
            writer.write((json.dumps(job) + "\n").encode())
            await writer.drain()
            stop = asyncio.ensure_future(execution.stop.wait())
            try:
                for _ in range(MAX_MESSAGES):
                    message = await self._next(execution, reader, stop, deadline)
                    if isinstance(message, Outcome):
                        return message
                    if message.get("type") == "result":
                        return self._result(execution, message)
                    reply = await self._handle(execution, admitted, message, deadline)
                    if reply is None:
                        return Outcome("failed", execution.id, error="protocol")
                    if reply:
                        writer.write((json.dumps(reply) + "\n").encode())
                        await writer.drain()
                return Outcome("failed", execution.id, error="protocol")
            finally:
                stop.cancel()
        finally:
            writer.close()

    async def _next(
        self,
        execution: _Execution,
        reader: asyncio.StreamReader,
        stop: "asyncio.Future[Any]",
        deadline: float,
    ) -> dict[str, Any] | Outcome:
        read = asyncio.ensure_future(reader.readline())
        done, _ = await asyncio.wait(
            {read, stop}, timeout=deadline - time.monotonic(), return_when="FIRST_COMPLETED"
        )
        if read not in done:
            read.cancel()
            status = "cancelled" if stop in done else "timeout"
            return Outcome(status, execution.id)
        try:
            line = read.result()
        except (ValueError, asyncio.LimitOverrunError):
            return Outcome("failed", execution.id, error="result_too_large")
        if not line:
            return Outcome("failed", execution.id, error="child_exited")
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            return Outcome("failed", execution.id, error="protocol")
        return message

    def _result(self, execution: _Execution, message: dict[str, Any]) -> Outcome:
        if execution.refusal is not None:
            return replace(execution.refusal, execution_id=execution.id)
        status, results = message.get("status"), message.get("results")
        if status not in CHILD_STATUSES or not isinstance(results, list):
            return Outcome("failed", execution.id, error="protocol")
        if not all(isinstance(result, dict) for result in results):
            return Outcome("failed", execution.id, error="protocol")
        error = message.get("error")
        return Outcome(
            status,
            execution.id,
            results=results if status == "ok" else [],
            error=error if isinstance(error, str) else None,
        )

    async def _handle(
        self, execution: _Execution, admitted: Admitted, message: dict[str, Any], deadline: float
    ) -> dict[str, Any] | None:
        if message.get("type") == "permit":
            paced, charge = message.get("paced"), message.get("charge")
            if not isinstance(paced, bool) or not isinstance(charge, bool):
                return None
            granted = await self._permit(execution, admitted, paced, charge, deadline)
            return {"type": "permit", "id": message.get("id"), "granted": granted}
        if message.get("type") == "release":
            permit = execution.permits.pop(message.get("permit"), None)  # type: ignore[arg-type]
            if permit is None:
                return None
            try:
                await asyncio.to_thread(self.coordinator.release_permit, permit)
            except CoordinatorUnavailable:
                pass  # it ends with the execution anyway
            return {}  # releases go unanswered
        return None

    async def _permit(
        self, execution: _Execution, admitted: Admitted, paced: bool, charge: bool, deadline: float
    ) -> int | None:
        """Waits for the retailer's pace only within the deadline; a full limit is Busy."""
        if execution.refusal is not None:
            return None
        policy = site_policy(execution.adapter.site)
        while True:
            try:
                result = await asyncio.to_thread(
                    self.coordinator.acquire_permit,
                    execution.adapter.retailer,
                    execution.id,
                    policy,
                    execution=admitted,
                    paced=paced,
                    charge=charge,
                )
            except CoordinatorUnavailable:
                execution.refusal = Outcome("unavailable")
                return None
            if isinstance(result, Permit):
                execution.permits[result.id] = result
                return result.id
            if isinstance(result, str):
                error = "stale_admission" if result == "stale" else "unconfigured_retailer"
                execution.refusal = Outcome("failed", error=error)
                return None
            wait = None if result.until is None else (result.until - datetime.now(UTC))
            if wait is None or time.monotonic() + wait.total_seconds() >= deadline:
                execution.refusal = Outcome("busy")
                return None
            await asyncio.sleep(max(wait.total_seconds(), 0))

    async def _stop(self, process: asyncio.subprocess.Process) -> bool:
        """SIGTERM, then SIGKILL, the child's session; True once none of it is left running."""
        session = process.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            members = _running_in_session(session)
            for pid in members:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            give_up = time.monotonic() + self.grace
            while _running_in_session(session) and time.monotonic() < give_up:
                await asyncio.sleep(0.02)
            if not _running_in_session(session):
                break
        try:
            await asyncio.wait_for(process.wait(), self.grace)
        except TimeoutError:
            return False
        return not _running_in_session(session)


def _log_tail(execution_id: str, outcome: Outcome, log: Path) -> None:
    try:
        tail = log.read_bytes()[-4096:].decode("utf-8", errors="replace")
    except OSError:
        tail = ""
    logger.warning("Search %s: %s %s\n%s", execution_id, outcome.status, outcome.error, tail)


def _running_in_session(session: int) -> list[int]:
    """Live (not zombie) processes in the session, from /proc."""
    pids = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
        except OSError:
            continue
        # The command name is parenthesized and may itself contain spaces or parentheses.
        fields = text[text.rindex(")") + 2 :].split()
        if int(fields[3]) == session and fields[0] not in ("Z", "X"):
            pids.append(int(stat.parent.name))
    return pids
