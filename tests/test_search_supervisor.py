"""The supervisor over disposable Postgres, with a scripted stand-in for the crawler child."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from groceries_scraper.coordination import (
    Admitted,
    Coordinator,
    CoordinatorAdmin,
    Permit,
    Policy,
)
from groceries_scraper.search.adapters import SearchAdapter, load_adapter
from groceries_scraper.search.supervisor import Accepted, Outcome, SearchRequest, Supervisor

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROLE = "scrape_coordinator_test"
PASSWORD = "coordinator-test"
FAKE_CHILD = [sys.executable, str(Path(__file__).with_name("fake_search_child.py"))]

ADAPTER = """
search:
  adapter_version: shop-live-v1
  retailer: {key: shop, name: Shop, hosts: [shop.example]}
  url: "https://shop.example/search?q={query}"
  page_type: search
  record: result
  verify: {page: "//main", empty: "//p[@class='none']"}
settings: {download_delay: 0.5, concurrent_requests_per_domain: 1}
records: {result: {key: [url]}}
page_types:
  search:
    record: result
    fields:
      title: {xpath: "//h2/text()", type: string, required: true}
      url: {xpath: "//a/@href", type: string, required: true}
"""


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def search(
    service: Supervisor, searched: SearchRequest, accepted: list[Accepted] | None = None
) -> Outcome:
    """One search, returning once its cleanup has finished too."""

    async def go() -> Outcome:
        try:
            return await service.search(searched, None if accepted is None else accepted.append)
        finally:
            await service.idle()

    return run(go())


def restricted_dsn() -> str:
    assert DSN is not None
    return make_conninfo(DSN, user=ROLE, password=PASSWORD)


@pytest.fixture
def admin() -> CoordinatorAdmin:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS coordination CASCADE")
    admin = CoordinatorAdmin(DSN)
    admin.prepare(ROLE, PASSWORD)
    admin.set_policies({"shop": Policy(0.5, 1)})
    return admin


@pytest.fixture
def adapter(tmp_path: Path) -> SearchAdapter:
    path = tmp_path / "shop.yaml"
    path.write_text(ADAPTER)
    return load_adapter(path)


@pytest.fixture
def coordinator(admin: CoordinatorAdmin) -> Iterator[Coordinator]:
    coordinator = Coordinator(restricted_dsn())
    yield coordinator
    coordinator.close()


def supervisor(adapter: SearchAdapter, tmp_path: Path, owner: str = "api-a") -> Supervisor:
    return Supervisor(
        Coordinator(restricted_dsn()),
        {"shop": adapter},
        owner,
        runner=FAKE_CHILD,
        workdir=tmp_path,
        grace=0.5,
    )


def request(
    query: str,
    *,
    key: str | None = None,
    request_id: str | None = None,
    budget_ms: int = 3000,
    admission_budget_ms: int = 2000,
    retailer: str = "shop",
    adapter_version: str = "shop-live-v1",
    location: str = "default",
) -> SearchRequest:
    return SearchRequest(
        request_id=request_id or str(uuid.uuid4()),
        execution_key=key or f"owner-1:{query}",
        retailer=retailer,
        adapter_version=adapter_version,
        query=query,
        context={"location": location},
        budget_ms=budget_ms,
        admission_budget_ms=admission_budget_ms,
        expires_at=datetime.now(UTC) + timedelta(milliseconds=budget_ms),
    )


def slot_is_free(coordinator: Coordinator) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    probe = coordinator.admit_search(str(uuid.uuid4()), "probe", "probe", deadline)
    if isinstance(probe, Admitted):
        assert coordinator.end_search(probe)
        return True
    return False


def running(marker: str) -> bool:
    found = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True)
    return found.returncode == 0


def marker() -> str:
    return f"{3000 + uuid.uuid4().int % 1000}.{uuid.uuid4().int % 10**6}"


# --- A fresh search, end to end ----------------------------------------------


def test_a_search_runs_one_child_and_frees_admission_after_it_ends(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    accepted: list[Accepted] = []

    outcome = search(supervisor(adapter, tmp_path), request("ok"), accepted)

    assert outcome.status == "ok"
    assert outcome.results == [{"title": "Milk 2L", "url": "https://shop.example/p/milk"}]
    assert [a.execution_id for a in accepted] == [outcome.execution_id]
    assert accepted[0].joined is False
    assert slot_is_free(coordinator)


def test_the_child_gets_only_an_allowlisted_environment_and_its_budget(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator, monkeypatch: Any
) -> None:
    monkeypatch.setenv("SCRAPE_SINK", "postgresql://scrape@postgres/scrape")
    monkeypatch.setenv("PGPASSWORD", "owner")
    monkeypatch.setenv("SCRAPE_COORDINATOR", restricted_dsn())
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "secret")

    outcome = search(supervisor(adapter, tmp_path), request("env", budget_ms=4000))

    (seen,) = outcome.results
    assert set(seen["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH"}
    assert Path(seen["cwd"]).is_relative_to(tmp_path)
    assert 0 < seen["budget"] < 4
    # Its scratch directory goes with it.
    assert not Path(seen["cwd"]).exists()


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"retailer": "other"}, "unsupported_retailer"),
        ({"adapter_version": "shop-live-v0"}, "unsupported_retailer"),
        ({"location": "sydney"}, "invalid_request"),
        ({"query": "   "}, "invalid_request"),
        ({"query": "x" * 201}, "invalid_request"),
        ({"budget_ms": 10_001}, "invalid_request"),
        ({"admission_budget_ms": 2_001}, "invalid_request"),
    ],
)
def test_invalid_input_is_refused_before_admission(
    adapter: SearchAdapter,
    tmp_path: Path,
    coordinator: Coordinator,
    changes: dict[str, Any],
    status: str,
) -> None:
    query = changes.pop("query", "ok")
    accepted: list[Accepted] = []

    outcome = search(supervisor(adapter, tmp_path), request(query, **changes), accepted)

    assert outcome.status == status
    assert accepted == [] and outcome.execution_id is None


def test_a_passed_expiry_starts_nothing(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    expired = request("ok")
    expired = SearchRequest(**{**expired.__dict__, "expires_at": datetime.now(UTC)})

    assert search(supervisor(adapter, tmp_path), expired).status == "timeout"


def test_an_unreachable_coordinator_fails_admission_closed(
    adapter: SearchAdapter, tmp_path: Path
) -> None:
    unreachable = Supervisor(
        Coordinator("postgresql://nobody@127.0.0.1:1/none", connect_timeout=1),
        {"shop": adapter},
        "api-a",
        runner=FAKE_CHILD,
        workdir=tmp_path,
    )

    assert search(unreachable, request("ok")).status == "unavailable"


# --- Capacity, coalescing and conflicts ---------------------------------------


def test_two_service_instances_admit_one_fresh_search(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    async def both() -> list[Outcome]:
        one = supervisor(adapter, tmp_path, "api-a")
        first = asyncio.create_task(one.search(request("slow 0.5")))
        await asyncio.sleep(0.3)
        other = supervisor(adapter, tmp_path, "api-b")
        second = await other.search(request("ok"))
        result = [await first, second]
        await one.idle()
        await other.idle()
        return result

    first, second = run(both())

    assert (first.status, second.status) == ("ok", "busy")


def test_identical_input_joins_the_running_execution_without_a_new_deadline(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    service = supervisor(adapter, tmp_path)
    accepted: list[Accepted] = []

    async def both() -> list[Outcome]:
        first = asyncio.create_task(service.search(request("slow 0.6", key="k"), accepted.append))
        await asyncio.sleep(0.2)
        later = request("slow 0.6", key="k", budget_ms=9000)
        second = await service.search(later, accepted.append)
        result = [await first, second]
        await service.idle()
        return result

    first, joined = run(both())

    assert first.status == joined.status == "ok"
    assert joined.execution_id == first.execution_id
    assert [a.joined for a in accepted] == [False, True]
    assert accepted[1].execution_expires_at == accepted[0].execution_expires_at


@pytest.mark.parametrize("reuse", ["key", "request_id"])
def test_active_identity_with_different_input_is_refused(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator, reuse: str
) -> None:
    service = supervisor(adapter, tmp_path)
    original = request("slow 0.6", key="k", request_id="r1")

    async def both() -> list[Outcome]:
        first = asyncio.create_task(service.search(original))
        await asyncio.sleep(0.2)
        if reuse == "key":
            other = request("ok", key="k")
        else:
            other = request("ok", key="k2", request_id="r1")
        second = await service.search(other)
        result = [await first, second]
        await service.idle()
        return result

    first, conflicting = run(both())

    assert first.status == "ok"
    assert conflicting.status == "invalid_request"
    assert conflicting.error == "conflicting_input"


def test_a_finished_request_id_never_starts_another_execution(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    service = supervisor(adapter, tmp_path)
    first = search(service, request("ok", request_id="r1"))

    again = search(service, request("ok", request_id="r1"))

    assert first.status == "ok"
    assert (again.status, again.error) == ("invalid_request", "request_reused")


# --- Permits through the supervisor -------------------------------------------


def test_search_waits_for_the_retailers_pace_inside_its_deadline(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    # A price Job's request just started: the next may start 0.5 s later.
    held = coordinator.acquire_permit("shop", "job-a", Policy(0.5, 2), lease=timedelta(minutes=1))
    assert isinstance(held, Permit)
    coordinator.release_permit(held)

    started = time.monotonic()
    outcome = search(supervisor(adapter, tmp_path), request("ok"))

    assert outcome.status == "ok"
    assert time.monotonic() - started >= 0.4


def test_search_is_busy_while_price_work_fills_the_retailers_concurrency(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    held = coordinator.acquire_permit("shop", "job-a", Policy(0, 1), lease=timedelta(minutes=1))
    assert isinstance(held, Permit)

    outcome = search(supervisor(adapter, tmp_path), request("ok"))

    assert outcome.status == "busy"
    assert slot_is_free(coordinator)


def test_a_recovered_executions_child_gets_no_permits(
    adapter: SearchAdapter, tmp_path: Path, admin: CoordinatorAdmin, coordinator: Coordinator
) -> None:
    accepted: list[Accepted] = []

    service = supervisor(adapter, tmp_path)

    async def recovered_midway() -> Outcome:
        searching = asyncio.create_task(
            service.search(request("permit-later 0.6"), accepted.append)
        )
        await asyncio.sleep(0.3)
        (execution,) = admin.active_searches()
        assert admin.recover_search(execution["execution_id"], execution["fence"], "api-a", "test")
        outcome = await searching
        await service.idle()
        return outcome

    outcome = run(recovered_midway())

    assert (outcome.status, outcome.error) == ("failed", "stale_admission")
    # Recovery already freed the slot; the stale owner's end changed nothing.
    assert slot_is_free(coordinator)


# --- Failure, deadline, detach and cleanup ------------------------------------


def test_a_crashed_child_fails_the_search(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    outcome = search(supervisor(adapter, tmp_path), request("crash"))

    assert (outcome.status, outcome.error) == ("failed", "child_exited")
    assert slot_is_free(coordinator)


def test_a_malformed_message_fails_the_search(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    outcome = search(supervisor(adapter, tmp_path), request("garbage"))

    assert (outcome.status, outcome.error) == ("failed", "protocol")
    assert slot_is_free(coordinator)


def test_an_oversize_result_fails_and_the_child_is_stopped(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    started = time.monotonic()
    outcome = search(supervisor(adapter, tmp_path), request("oversize"))

    assert (outcome.status, outcome.error) == ("failed", "result_too_large")
    assert time.monotonic() - started < 2
    assert slot_is_free(coordinator)


@pytest.mark.parametrize("query", ["hang", "stubborn"])
def test_the_deadline_stops_the_child_even_if_it_ignores_sigterm(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator, query: str
) -> None:
    started = time.monotonic()
    outcome = search(supervisor(adapter, tmp_path), request(query, budget_ms=800))

    assert outcome.status == "timeout"
    assert 0.8 <= time.monotonic() - started < 2.5
    assert slot_is_free(coordinator)


def test_a_late_completion_is_never_a_result(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    outcome = search(supervisor(adapter, tmp_path), request("late", budget_ms=800))

    assert outcome.status == "timeout" and outcome.results == []


def test_cleanup_stops_the_childs_descendants_before_freeing_admission(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    timed_out, finished = marker(), marker()

    first = search(supervisor(adapter, tmp_path), request(f"grandchild {timed_out}", budget_ms=800))
    second = search(supervisor(adapter, tmp_path), request(f"grandchild {finished} result"))

    assert (first.status, second.status) == ("timeout", "ok")
    assert not running(timed_out) and not running(finished)
    assert slot_is_free(coordinator)


def test_the_last_caller_detaching_stops_the_execution(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    descendant = marker()

    service = supervisor(adapter, tmp_path)

    async def detach() -> float:
        searching = asyncio.create_task(
            service.search(request(f"grandchild {descendant}", budget_ms=9000))
        )
        await asyncio.sleep(0.5)
        searching.cancel()
        detached = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await searching
        # Detaching returns at once; cleanup finishes in the background.
        while not slot_is_free(coordinator):
            await asyncio.sleep(0.05)
        return time.monotonic() - detached

    assert run(detach()) < 2
    assert not running(descendant)


def test_a_joined_execution_continues_for_the_callers_that_remain(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    service = supervisor(adapter, tmp_path)

    async def one_detaches() -> Outcome:
        first = asyncio.create_task(service.search(request("slow 0.8", key="k")))
        await asyncio.sleep(0.2)
        second = asyncio.create_task(service.search(request("slow 0.8", key="k")))
        await asyncio.sleep(0.1)
        first.cancel()
        outcome = await second
        await service.idle()
        return outcome

    outcome = run(one_detaches())

    assert outcome.status == "ok" and outcome.joined


def test_a_caller_arriving_after_everyone_detached_is_not_handed_the_cancellation(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    service = supervisor(adapter, tmp_path)

    async def rejoin() -> Outcome:
        first = asyncio.create_task(service.search(request("hang", key="k", budget_ms=9000)))
        await asyncio.sleep(0.5)
        first.cancel()
        await asyncio.sleep(0)
        # Same work, but the stopping execution is no longer joinable.
        outcome = await service.search(request("hang", key="k", budget_ms=9000))
        await service.idle()
        return outcome

    assert run(rejoin()).status == "busy"


def test_a_joiner_waits_only_until_its_own_deadline(
    adapter: SearchAdapter, tmp_path: Path, coordinator: Coordinator
) -> None:
    service = supervisor(adapter, tmp_path)

    async def short_joiner() -> tuple[Outcome, Outcome, float]:
        first = asyncio.create_task(service.search(request("slow 1.2", key="k")))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        joined = await service.search(request("slow 1.2", key="k", budget_ms=400))
        waited = time.monotonic() - started
        outcome = await first
        await service.idle()
        return outcome, joined, waited

    first, joined, waited = run(short_joiner())

    assert (first.status, joined.status) == ("ok", "timeout")
    assert waited < 0.8
