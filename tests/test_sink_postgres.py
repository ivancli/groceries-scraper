import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sink_runs import saved_run

from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks import export_run, open_sink

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)


@pytest.fixture
def db() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP TABLE IF EXISTS scrape_records, scrape_runs")
        yield connection


def _runs(db: psycopg.Connection[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT site, location, run_id, health, manifest->>'run_id' FROM scrape_runs"
        " ORDER BY run_id"
    ).fetchall()


def _records(db: psycopg.Connection[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT run_id, record_type, record_key, data, meta FROM scrape_records"
        " ORDER BY run_id, record_type, data::text"
    ).fetchall()


def test_a_run_and_its_records_are_stored_with_record_keys_apart_from_meta(
    tmp_path: Path, db: psycopg.Connection[tuple[Any, ...]]
) -> None:
    run = saved_run(
        tmp_path,
        {
            "product": [{"sku": "a", "price": 1.5}, {"sku": "b", "price": None}],
            "promotion": [{"label": "2 for 1"}],
        },
        health="degraded",
    )

    sink = open_sink(DSN or "")
    sink.prepare()
    assert export_run(run, [sink])

    run_id = "20260101T000000Z-abc123"
    assert _runs(db) == [("shop", "default", run_id, "degraded", run_id)]
    meta = {"run_id": run_id}
    assert _records(db) == [
        (run_id, "product", {"sku": "a"}, {"sku": "a", "price": 1.5}, meta),
        (run_id, "product", {"sku": "b"}, {"sku": "b", "price": None}, meta),
        (run_id, "promotion", None, {"label": "2 for 1"}, meta),
    ]


def test_re_exporting_a_run_replaces_only_that_runs_rows(
    tmp_path: Path, db: psycopg.Connection[tuple[Any, ...]]
) -> None:
    sink = open_sink(DSN or "")
    sink.prepare()
    first = saved_run(tmp_path, {"product": [{"sku": "a"}]}, run_id="1")
    second = saved_run(tmp_path, {"product": [{"sku": "a"}, {"sku": "b"}]}, run_id="2")
    sink.export(SavedRun.load(first))
    sink.export(SavedRun.load(second))
    (second / "records/product.jsonl").write_text('{"sku": "c", "_meta": {}}\n')

    sink.export(SavedRun.load(second))

    assert [(row[0], row[2]) for row in _runs(db)] == [("shop", "1"), ("shop", "2")]
    assert [(row[0], row[3]) for row in _records(db)] == [("1", {"sku": "a"}), ("2", {"sku": "c"})]


def test_prepare_creates_the_tables_once(db: psycopg.Connection[tuple[Any, ...]]) -> None:
    sink = open_sink(DSN or "")

    sink.prepare()
    sink.prepare()

    assert _runs(db) == []
    assert _records(db) == []


def test_nul_characters_postgres_cannot_store_are_replaced(
    tmp_path: Path, db: psycopg.Connection[tuple[Any, ...]]
) -> None:
    run = saved_run(tmp_path, {"product": [{"sku": "a", "name": "x\u0000y", "tags": ["\u0000"]}]})
    sink = open_sink(DSN or "")
    sink.prepare()

    sink.export(SavedRun.load(run))

    assert [row[3] for row in _records(db)] == [
        {"sku": "a", "name": "x\ufffdy", "tags": ["\ufffd"]}
    ]


def test_a_run_is_stored_with_its_location(
    tmp_path: Path, db: psycopg.Connection[tuple[Any, ...]]
) -> None:
    run = saved_run(tmp_path, {"product": [{"sku": "a"}]}, location="melb")

    sink = open_sink(DSN or "")
    sink.prepare()
    assert export_run(run, [sink])

    run_id = "20260101T000000Z-abc123"
    assert _runs(db) == [("shop", "melb", run_id, "ok", run_id)]


def test_prepare_adds_the_location_column_to_tables_from_before_locations(
    tmp_path: Path, db: psycopg.Connection[tuple[Any, ...]]
) -> None:
    db.execute(
        "CREATE TABLE scrape_runs (site text NOT NULL, run_id text NOT NULL,"
        " health text NOT NULL, manifest jsonb NOT NULL,"
        " exported_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (site, run_id))"
    )
    db.execute(
        "INSERT INTO scrape_runs (site, run_id, health, manifest)"
        " VALUES ('shop', 'old', 'ok', '{}')"
    )
    db.execute(
        "CREATE TABLE scrape_records (site text NOT NULL, run_id text NOT NULL,"
        " record_type text NOT NULL, record_key jsonb, data jsonb NOT NULL, meta jsonb NOT NULL,"
        " FOREIGN KEY (site, run_id) REFERENCES scrape_runs ON DELETE CASCADE)"
    )

    sink = open_sink(DSN or "")
    sink.prepare()
    assert export_run(saved_run(tmp_path, {}, location="melb"), [sink])

    assert [row[:3] for row in _runs(db)] == [
        ("shop", "melb", "20260101T000000Z-abc123"),
        ("shop", "default", "old"),
    ]
