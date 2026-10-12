import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from groceries_scraper.coordination import (
    Admitted,
    Coordinator,
    CoordinatorAdmin,
    CoordinatorUnavailable,
    Permit,
    Policy,
    Wait,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROLE = "scrape_coordinator_test"
PASSWORD = "coordinator-test"


def soon(seconds: float = 10) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture
def admin() -> Iterator[CoordinatorAdmin]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS coordination CASCADE")
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        if exists.fetchone():
            connection.execute(f"DROP OWNED BY {ROLE}")
            connection.execute(f"DROP ROLE {ROLE}")
    admin = CoordinatorAdmin(DSN)
    admin.prepare(ROLE, PASSWORD)
    yield admin


def restricted_dsn() -> str:
    assert DSN is not None
    return make_conninfo(DSN, user=ROLE, password=PASSWORD)


@pytest.fixture
def coordinator(admin: CoordinatorAdmin) -> Iterator[Coordinator]:
    coordinator = Coordinator(restricted_dsn())
    yield coordinator
    coordinator.close()


def admitted(result: Any) -> Admitted:
    assert isinstance(result, Admitted), result
    return result


# --- Fresh-search admission -------------------------------------------------


def test_one_fresh_search_is_admitted_and_distinct_work_is_busy(
    coordinator: Coordinator,
) -> None:
    first = admitted(coordinator.admit_search("e1", "coles:milk", "api-a", soon()))

    assert coordinator.admit_search("e2", "coles:bread", "api-a", soon()) == "busy"
    assert first.execution_id == "e1"


def test_readmitting_the_same_execution_keeps_its_fence_and_deadline(
    coordinator: Coordinator,
) -> None:
    deadline = soon(5)
    first = admitted(coordinator.admit_search("e1", "coles:milk", "api-a", deadline))

    again = coordinator.admit_search("e1", "coles:milk", "api-a", soon(60))

    assert again == first
    assert first.deadline_at == deadline


def test_the_same_execution_id_for_other_work_or_owner_is_a_replay(
    coordinator: Coordinator,
) -> None:
    admitted(coordinator.admit_search("e1", "coles:milk", "api-a", soon()))

    assert coordinator.admit_search("e1", "coles:bread", "api-a", soon()) == "replayed"
    assert coordinator.admit_search("e1", "coles:milk", "api-b", soon()) == "replayed"


def test_a_passed_deadline_is_never_admitted(coordinator: Coordinator) -> None:
    assert coordinator.admit_search("e1", "w", "api-a", soon(-1)) == "expired"
    # Nothing was reserved by the refusal.
    admitted(coordinator.admit_search("e2", "w", "api-a", soon()))


def test_ending_after_cleanup_frees_the_slot_and_the_execution_never_replays(
    coordinator: Coordinator,
) -> None:
    first = admitted(coordinator.admit_search("e1", "w", "api-a", soon()))

    assert coordinator.end_search(first) is True
    second = admitted(coordinator.admit_search("e2", "w", "api-a", soon()))

    assert second.fence > first.fence
    assert coordinator.end_search(second) is True
    assert coordinator.admit_search("e1", "w", "api-a", soon()) == "replayed"


def test_lease_expiry_alone_keeps_the_slot_occupied(coordinator: Coordinator) -> None:
    admitted(coordinator.admit_search("e1", "w", "api-a", soon(0.2)))
    deadline_passed = soon(0.5)
    while datetime.now(UTC) < deadline_passed:
        pass

    # The deadline passed, but nothing verified that the execution was cleaned up.
    assert coordinator.admit_search("e2", "w", "api-b", soon()) == "busy"


def test_a_stale_fence_cannot_end_the_current_execution(
    admin: CoordinatorAdmin, coordinator: Coordinator
) -> None:
    old = admitted(coordinator.admit_search("e1", "w", "api-a", soon()))
    assert admin.recover_search("e1", old.fence, "api-a", "api-a pod deleted") is True
    current = admitted(coordinator.admit_search("e2", "w", "api-b", soon()))

    # The old owner wakes up after recovery and reports its cleanup.
    assert coordinator.end_search(old) is False
    assert coordinator.admit_search("e3", "w", "api-b", soon()) == "busy"
    assert coordinator.end_search(current) is True


def test_simultaneous_admissions_from_separate_services_admit_exactly_one(
    admin: CoordinatorAdmin,
) -> None:
    def admit(n: int) -> object:
        coordinator = Coordinator(restricted_dsn())
        try:
            return coordinator.admit_search(f"e{n}", f"work{n}", f"api-{n}", soon())
        finally:
            coordinator.close()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(admit, range(8)))

    assert sum(isinstance(result, Admitted) for result in results) == 1
    assert results.count("busy") == 7


def test_an_unreachable_coordinator_fails_admission_closed() -> None:
    coordinator = Coordinator("postgresql://nobody@127.0.0.1:1/none", connect_timeout=1)

    with pytest.raises(CoordinatorUnavailable):
        coordinator.admit_search("e1", "w", "api-a", soon())


# --- Retailer request permits ------------------------------------------------

PACED = Policy(min_interval=0.3, max_concurrent=5)
LEASE = timedelta(minutes=5)


def granted(result: Any) -> Permit:
    assert isinstance(result, Permit), result
    return result


def test_permits_for_a_retailer_are_paced_by_its_interval(coordinator: Coordinator) -> None:
    first = granted(coordinator.acquire_permit("coles", "job-a", PACED, lease=LEASE))

    waiting = coordinator.acquire_permit("coles", "job-b", PACED, lease=LEASE)

    assert isinstance(waiting, Wait) and waiting.until is not None
    assert waiting.until - first.granted_at == timedelta(seconds=0.3)
    # Another retailer has its own pace.
    granted(coordinator.acquire_permit("aldi", "job-b", PACED, lease=LEASE))


def test_a_waiting_holder_is_granted_once_the_interval_passes(coordinator: Coordinator) -> None:
    granted(coordinator.acquire_permit("coles", "job-a", PACED, lease=LEASE))
    waiting = coordinator.acquire_permit("coles", "job-b", PACED, lease=LEASE)
    assert isinstance(waiting, Wait) and waiting.until is not None
    while datetime.now(UTC) < waiting.until:
        pass

    granted(coordinator.acquire_permit("coles", "job-b", PACED, lease=LEASE))


def test_permits_in_flight_are_bounded_until_released(coordinator: Coordinator) -> None:
    single = Policy(min_interval=0, max_concurrent=1)
    first = granted(coordinator.acquire_permit("coles", "job-a", single, lease=LEASE))

    assert coordinator.acquire_permit("coles", "job-b", single, lease=LEASE) == Wait(None)
    coordinator.release_permit(first)
    granted(coordinator.acquire_permit("coles", "job-b", single, lease=LEASE))


def test_a_caller_cannot_loosen_the_retailers_configured_policy(
    admin: CoordinatorAdmin, coordinator: Coordinator
) -> None:
    admin.set_policies({"coles": Policy(min_interval=0, max_concurrent=1)})
    loose = Policy(min_interval=0, max_concurrent=10)
    granted(coordinator.acquire_permit("coles", "search", loose, lease=LEASE))

    assert coordinator.acquire_permit("coles", "search", loose, lease=LEASE) == Wait(None)


def test_a_crashed_jobs_permit_lapses_with_its_lease(coordinator: Coordinator) -> None:
    single = Policy(min_interval=0, max_concurrent=1)
    lease = timedelta(seconds=0.2)
    granted(coordinator.acquire_permit("coles", "job-a", single, lease=lease))
    lapsed = soon(0.3)
    while datetime.now(UTC) < lapsed:
        pass

    granted(coordinator.acquire_permit("coles", "job-b", single, lease=lease))


def test_an_unpaced_permit_counts_in_flight_but_leaves_the_pace(
    coordinator: Coordinator,
) -> None:
    pair = Policy(min_interval=0.3, max_concurrent=2)
    first = granted(coordinator.acquire_permit("coles", "job-a", pair, lease=LEASE))

    # A page's script, say, right after its navigation.
    granted(coordinator.acquire_permit("coles", "job-a", pair, lease=LEASE, paced=False))

    assert coordinator.acquire_permit("coles", "job-b", pair, lease=LEASE, paced=False) == Wait(
        None
    )
    coordinator.release_permit(first)
    waiting = coordinator.acquire_permit("coles", "job-b", pair, lease=LEASE)
    assert isinstance(waiting, Wait) and waiting.until is not None
    assert waiting.until - first.granted_at == timedelta(seconds=0.3)


def test_a_charged_request_counts_even_beyond_the_limits(coordinator: Coordinator) -> None:
    single = Policy(min_interval=0.3, max_concurrent=1)
    first = granted(coordinator.acquire_permit("coles", "job-a", single, lease=LEASE))

    # A browser redirect hop: it already happened, so it can only be accounted for.
    charged = granted(
        coordinator.acquire_permit("coles", "job-a", single, lease=LEASE, charge=True)
    )
    coordinator.release_permit(first)

    waiting = coordinator.acquire_permit("coles", "job-b", single, lease=LEASE)
    assert waiting == Wait(None)
    coordinator.release_permit(charged)
    waiting = coordinator.acquire_permit("coles", "job-b", single, lease=LEASE)
    # The early hop is owed: the pace moves on from the first permit's next start.
    assert isinstance(waiting, Wait) and waiting.until is not None
    assert waiting.until - first.granted_at == timedelta(seconds=0.6)


def test_search_permits_need_the_current_admission(coordinator: Coordinator) -> None:
    search = admitted(coordinator.admit_search("e1", "w", "api-a", soon()))
    stale = Admitted("e1", search.fence + 1, search.deadline_at)

    granted(coordinator.acquire_permit("coles", "e1", PACED, execution=search))
    assert coordinator.acquire_permit("aldi", "e1", PACED, execution=stale) == "stale"


def test_search_gets_no_permits_after_its_deadline(coordinator: Coordinator) -> None:
    search = admitted(coordinator.admit_search("e1", "w", "api-a", soon(0.2)))
    passed = soon(0.3)
    while datetime.now(UTC) < passed:
        pass

    assert coordinator.acquire_permit("coles", "e1", PACED, execution=search) == "stale"


def test_search_permits_hold_until_the_execution_ends(coordinator: Coordinator) -> None:
    single = Policy(min_interval=0, max_concurrent=1)
    search = admitted(coordinator.admit_search("e1", "w", "api-a", soon()))
    granted(coordinator.acquire_permit("coles", "e1", single, execution=search))

    assert coordinator.acquire_permit("coles", "job-a", single, lease=LEASE) == Wait(None)
    assert coordinator.end_search(search) is True
    granted(coordinator.acquire_permit("coles", "job-a", single, lease=LEASE))


# --- Credential isolation ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM dispatch.price_observations",
        "SELECT * FROM dispatch.outbox",
        "SELECT * FROM dispatch.collector_state",
        "SELECT * FROM scrape_records",
        "SELECT * FROM scrape_outcomes",
        "SELECT * FROM coordination.search_executions",
        "DELETE FROM coordination.permits",
        "UPDATE coordination.retailer_policies SET max_concurrent = 100",
        "SELECT coordination.recover_search('e1', 1, 'api-a', 'not the operator')",
    ],
)
def test_the_coordination_role_reaches_only_the_coordination_functions(
    admin: CoordinatorAdmin, query: str
) -> None:
    from groceries_scraper.dispatch.store import DispatchStore
    from groceries_scraper.run.sinks.postgres import PostgresSink

    assert DSN is not None
    PostgresSink(DSN).prepare()
    DispatchStore(DSN).prepare()

    with psycopg.connect(restricted_dsn(), autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(query)
