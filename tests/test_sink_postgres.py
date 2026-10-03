import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sink_runs import saved_run

from groceries_scraper.run.sinks import RunExport, export_run, open_sink

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
        "SELECT site, run_id, health, manifest->>'run_id' FROM scrape_runs ORDER BY run_id"
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

    assert export_run(run, [open_sink(DSN or "")])

    run_id = "20260101T000000Z-abc123"
    assert _runs(db) == [("shop", run_id, "degraded", run_id)]
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
    first = saved_run(tmp_path, {"product": [{"sku": "a"}]}, run_id="1")
    second = saved_run(tmp_path, {"product": [{"sku": "a"}, {"sku": "b"}]}, run_id="2")
    sink.publish(RunExport.load(first))
    sink.publish(RunExport.load(second))
    (second / "records/product.jsonl").write_text('{"sku": "c", "_meta": {}}\n')

    sink.publish(RunExport.load(second))

    assert [row[:2] for row in _runs(db)] == [("shop", "1"), ("shop", "2")]
    assert [(row[0], row[3]) for row in _records(db)] == [("1", {"sku": "a"}), ("2", {"sku": "c"})]
