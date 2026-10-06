import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sink_runs import saved_run

from groceries_scraper.dispatch.store import DispatchStore
from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks.postgres import PostgresSink
from groceries_scraper.run.supply import write_jsonl

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
REF = "walnuts@default"
URL = "https://shop.example/walnuts"


@pytest.fixture
def db() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS dispatch CASCADE")
        connection.execute("DROP TABLE IF EXISTS scrape_outcomes, scrape_records, scrape_runs")
        yield connection


@pytest.fixture
def store(db: psycopg.Connection[tuple[Any, ...]]) -> DispatchStore:
    sink = PostgresSink(DSN or "")
    sink.prepare()
    store = DispatchStore(DSN or "")
    store.prepare()
    db.execute(
        "INSERT INTO dispatch.watch_entries (ref, url, location, etag) VALUES (%s, %s, %s, %s)",
        (REF, URL, "default", '"1"'),
    )
    return store


def exported_run(
    root: Path,
    store: DispatchStore,
    run_id: str,
    at: str,
    *,
    record: dict[str, Any] | None = None,
    outcome: str = "ok",
    register: bool = True,
) -> SavedRun:
    path = saved_run(
        root,
        {},
        run_id=run_id,
        location="default",
        outcomes=[
            {"ref": REF, "outcome": outcome, "error": "HTTP 503" if outcome == "failed" else None}
        ],
    )
    if record is not None:
        write_jsonl(
            path / "records/product.jsonl",
            [
                {
                    "url": URL,
                    "name": "Natural Walnuts 500g",
                    "price": 5.49,
                    **record,
                    "_meta": {"ref": REF, "scraped_at": at, "capture_no": 7},
                }
            ],
        )
    run = SavedRun.load(path)
    PostgresSink(DSN or "").export(run)
    # The Sink timestamps unsuccessful outcomes on export.
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        connection.execute("UPDATE scrape_outcomes SET at = %s WHERE run_id = %s", (at, run_id))
    if register:
        store.record_dispatch(
            site="shop",
            location="default",
            run_id=run_id,
            job_name=f"job-{run_id}",
            refs=[REF],
            created_at=datetime.fromisoformat(at),
        )
    return run


def items(db: psycopg.Connection[tuple[Any, ...]], kind: str) -> list[dict[str, Any]]:
    return [
        row[0]
        for row in db.execute(
            "SELECT payload FROM dispatch.outbox WHERE kind = %s ORDER BY created_at, id", (kind,)
        ).fetchall()
    ]


def test_success_keeps_cents_and_evidence_and_enqueues_collector_items(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "first", "2026-10-05T22:00:00Z", record={})

    assert store.ingest("shop", "first")

    observation = db.execute(
        "SELECT shelf_price_cents, is_deal, store_verified, run_id, capture_no"
        " FROM dispatch.price_observations"
    ).fetchone()
    assert observation == (549, None, None, "first", 7)
    change = items(db, "change")
    assert len(change) == 1
    assert change[0]["state"] == {
        "shelf_price_cents": 549,
        "regular_price_cents": None,
        "is_deal": None,
        "unit_price_cents": None,
        "unit_basis": None,
        "unit_price_text": None,
        "price_kind": None,
        "availability": None,
        "store_verified": None,
        "promo_text": None,
    }
    assert change[0]["product"] == {
        "name": "Natural Walnuts 500g",
        "brand": None,
        "size_text": None,
        "product_url": URL,
    }
    assert change[0]["observed_at"] == "2026-10-05T22:00:00Z"
    assert change[0]["id"] == "03457a7827a4f0b3939f935699f678acca260c4f95b7f1282dde6f9314e53a53"
    assert items(db, "heartbeat")[0]["last_checked_at"] == "2026-10-05T22:00:00Z"
    assert items(db, "health") == []


def test_non_aud_is_a_failed_check_and_preserves_the_last_successful_state(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "first", "2026-10-05T22:00:00Z", record={})
    store.ingest("shop", "first")
    exported_run(tmp_path, store, "foreign", "2026-10-05T23:00:00Z", record={"currency": "USD"})

    store.ingest("shop", "foreign")

    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (1,)
    assert len(items(db, "change")) == 1

    check = db.execute(
        "SELECT outcome, error FROM dispatch.checks WHERE run_id = 'foreign'"
    ).fetchone()
    assert check is not None and check[0] == "failed" and "AUD" in check[1]
    assert db.execute(
        "SELECT health, consecutive_failures, last_success_at FROM dispatch.latest_state"
    ).fetchone() == ("failing", 1, datetime(2026, 10, 5, 22, tzinfo=UTC))
    assert items(db, "health")[0]["health"] == "failing"


def test_ingestion_ignores_manual_runs_replays_and_unfinished_exports_and_is_idempotent(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    manual = exported_run(
        tmp_path, store, "manual", "2026-10-05T22:00:00Z", record={}, register=False
    )
    assert not store.ingest("shop", manual.run_id)
    replay = exported_run(tmp_path, store, "replay", "2026-10-05T22:00:00Z", record={})
    manifest = {**replay.manifest, "replay_of": {"site": "shop", "run_id": "manual"}}
    (replay.path / "run.json").write_text(json.dumps(manifest))
    PostgresSink(DSN or "").export(SavedRun.load(replay.path))
    assert not store.ingest("shop", replay.run_id)
    store.record_dispatch(
        site="shop",
        location="default",
        run_id="pending",
        job_name="job-pending",
        refs=[REF],
        created_at=datetime.now(UTC),
    )
    assert not store.ingest("shop", "pending")
    assert db.execute("SELECT count(*) FROM dispatch.checks").fetchone() == (0,)
    assert items(db, "change") == []

    exported_run(tmp_path, store, "real", "2026-10-05T22:00:00Z", record={})
    assert store.ingest("shop", "real")
    before = db.execute("SELECT * FROM dispatch.outbox ORDER BY id").fetchall()
    assert not store.ingest("shop", "real")
    assert db.execute("SELECT * FROM dispatch.outbox ORDER BY id").fetchall() == before
    assert db.execute("SELECT count(*) FROM dispatch.checks").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (1,)


def test_walnut_week_keeps_every_check_but_sends_four_changes_two_health_and_daily_heartbeat(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    for day, price in enumerate([5.49, 5.49, 4.49, 4.49, 4.99, 5.49, 5.49]):
        for hour in range(3):
            run_id = f"day-{day}-check-{hour}"
            at = (start + timedelta(days=day, hours=hour)).isoformat()
            failed = day == 3 and hour == 1
            exported_run(
                tmp_path,
                store,
                run_id,
                at,
                record=None if failed else {"price": price},
                outcome="failed" if failed else "ok",
            )
            assert store.ingest("shop", run_id)

    assert db.execute("SELECT count(*) FROM dispatch.checks").fetchone() == (21,)
    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (20,)
    assert [item["state"]["shelf_price_cents"] for item in items(db, "change")] == [
        549,
        449,
        499,
        549,
    ]
    health = items(db, "health")
    assert [item["health"] for item in health] == ["failing", "ok"]
    assert health[0]["since"] == "2026-10-08T23:00:00Z"
    assert health[1]["since"] is None
    assert len(items(db, "heartbeat")) == 7
    assert db.execute(
        "SELECT health, failing_since, consecutive_failures FROM dispatch.latest_state"
    ).fetchone() == ("ok", None, 0)


def test_heartbeats_use_sydney_midnight_and_each_requested_success(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    times = ["2026-10-06T12:30:00Z", "2026-10-06T13:30:00Z", "2026-10-06T14:30:00Z"]
    for index, at in enumerate(times):
        exported_run(tmp_path, store, str(index), at, record={})
        store.ingest("shop", str(index))
    assert len(items(db, "heartbeat")) == 2
    db.execute("UPDATE dispatch.watch_entries SET check_soon = true")
    for index in range(2):
        run_id = f"requested-{index}"
        at = f"2026-10-06T{15 + index}:30:00Z"
        exported_run(tmp_path, store, run_id, at, record={})
        store.ingest("shop", run_id)

    assert len(items(db, "heartbeat")) == 4
    assert len(items(db, "change")) == 1


def test_a_wrong_location_or_incomplete_supply_never_commits_an_ingestion(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "wrong", "2026-10-05T22:00:00Z", record={})
    db.execute("UPDATE dispatch.dispatches SET location = 'melbourne'")
    with pytest.raises(ValueError, match="Location"):
        store.ingest("shop", "wrong")
    db.execute(
        "UPDATE dispatch.dispatches SET location = 'default', refs = ARRAY[%s, 'missing']", (REF,)
    )
    with pytest.raises(ValueError, match="Outcomes"):
        store.ingest("shop", "wrong")
    assert db.execute("SELECT count(*) FROM dispatch.checks").fetchone() == (0,)
    assert db.execute("SELECT ingested_at FROM dispatch.dispatches").fetchone() == (None,)


def test_an_outbox_failure_rolls_back_the_whole_run_and_allows_a_retry(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "rollback", "2026-10-05T22:00:00Z", record={})
    db.execute(
        "ALTER TABLE dispatch.outbox ADD CONSTRAINT reject_heartbeat CHECK (kind <> 'heartbeat')"
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        store.ingest("shop", "rollback")

    for table in ["checks", "price_observations", "latest_state", "outbox"]:
        assert db.execute(f"SELECT count(*) FROM dispatch.{table}").fetchone() == (0,)
    assert db.execute("SELECT ingested_at FROM dispatch.dispatches").fetchone() == (None,)
    db.execute("ALTER TABLE dispatch.outbox DROP CONSTRAINT reject_heartbeat")
    assert store.ingest("shop", "rollback")


def test_state_changes_include_optional_facts_but_not_product_descriptions(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    records: list[dict[str, Any]] = [
        {},
        {"is_deal": False},
        {"is_deal": False, "regular_price": 6.49},
        {"is_deal": False, "regular_price": 6.49, "name": "Walnuts renamed"},
    ]
    for index, record in enumerate(records):
        exported_run(tmp_path, store, str(index), f"2026-10-05T{20 + index}:00:00Z", record=record)
        store.ingest("shop", str(index))

    changes = items(db, "change")
    assert len(changes) == 3
    assert [item["state"]["is_deal"] for item in changes] == [None, False, False]
    assert changes[-1]["state"]["regular_price_cents"] == 649
    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (4,)


def test_each_distinct_tracker_outcome_is_enqueued_once_and_failure_streaks_are_kept(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    for index, outcome in enumerate(
        ["blocked", "blocked", "not_found", "failed", "skipped", "blocked"]
    ):
        exported_run(
            tmp_path, store, str(index), f"2026-10-05T{index + 10}:00:00Z", outcome=outcome
        )
        store.ingest("shop", str(index))

    assert [item["outcome"] for item in items(db, "outcome")] == [
        "blocked",
        "not_found",
    ]
    assert [item["health"] for item in items(db, "health")] == ["failing"]
    assert items(db, "change") == []
    assert items(db, "heartbeat") == []
    assert db.execute(
        "SELECT consecutive_failures, failing_since FROM dispatch.latest_state"
    ).fetchone() == (6, datetime(2026, 10, 5, 10, tzinfo=UTC))


def test_prepare_is_repeatable_without_losing_history(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "first", "2026-10-05T22:00:00Z", record={})
    store.ingest("shop", "first")
    before = db.execute("SELECT * FROM dispatch.price_observations").fetchall()

    store.prepare()
    store.prepare()

    assert db.execute("SELECT * FROM dispatch.price_observations").fetchall() == before


def test_concurrent_ingestion_commits_the_run_once(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    exported_run(tmp_path, store, "concurrent", "2026-10-05T22:00:00Z", record={})
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: store.ingest("shop", "concurrent"), range(4)))

    assert sum(results) == 1
    assert db.execute("SELECT count(*) FROM dispatch.checks").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (1,)
    assert len(items(db, "change")) == len(items(db, "heartbeat")) == 1


def test_delayed_runs_keep_history_without_replacing_newer_state_or_repeating_heartbeats(
    tmp_path: Path,
    db: psycopg.Connection[tuple[Any, ...]],
    store: DispatchStore,
) -> None:
    scenarios: list[tuple[str, str, dict[str, Any] | None, str]] = [
        ("newer", "2026-10-06T22:00:00Z", {"price": 4.49}, "ok"),
        ("older", "2026-10-05T22:00:00Z", {"price": 5.49}, "ok"),
        ("old-failure", "2026-10-06T00:00:00Z", None, "failed"),
        ("current", "2026-10-06T23:00:00Z", {"price": 4.49}, "ok"),
        ("old-repeat", "2026-10-05T23:00:00Z", {"price": 5.49}, "ok"),
    ]
    for run_id, at, record, outcome in scenarios:
        exported_run(tmp_path, store, run_id, at, record=record, outcome=outcome)
        store.ingest("shop", run_id)

    assert db.execute("SELECT count(*) FROM dispatch.price_observations").fetchone() == (4,)
    assert len(items(db, "change")) == 1
    assert len(items(db, "heartbeat")) == 2
    assert items(db, "health") == []
    assert db.execute(
        "SELECT health, last_success_at, last_attempt_at, last_heartbeat_date, consecutive_failures"
        " FROM dispatch.latest_state"
    ).fetchone() == (
        "ok",
        datetime(2026, 10, 6, 23, tzinfo=UTC),
        datetime(2026, 10, 6, 23, tzinfo=UTC),
        datetime(2026, 10, 7).date(),
        0,
    )
