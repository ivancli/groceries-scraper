import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sink_runs import saved_run
from typer.testing import CliRunner

from groceries_scraper.cli import app
from groceries_scraper.dispatch.janitor import Swept, sweep
from groceries_scraper.dispatch.store import DispatchStore
from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks.postgres import PostgresSink
from groceries_scraper.run.supply import write_jsonl

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to a disposable database")
NOW = datetime(2026, 10, 8, tzinfo=UTC)
REF = "walnuts@default"


@pytest.fixture
def db() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS dispatch CASCADE")
        connection.execute("DROP TABLE IF EXISTS scrape_outcomes, scrape_records, scrape_runs")
        PostgresSink(DSN).prepare()
        DispatchStore(DSN).prepare()
        yield connection


def run_dir(
    root: Path,
    days_old: int,
    health: str | None,
    suffix: str,
    *,
    dispatched: bool = True,
    outcomes: list[dict[str, Any]] | None = None,
) -> Path:
    created_at = NOW - timedelta(days=days_old)
    run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{suffix}"
    path = saved_run(root, {}, health=health, run_id=run_id, location="default", outcomes=outcomes)
    if dispatched:
        DispatchStore(DSN).record_dispatch(
            site="shop",
            location="default",
            run_id=run_id,
            job_name=f"job-{run_id}",
            refs=[REF],
            created_at=created_at,
        )
    return path


def ingested_run(root: Path, days_old: int, suffix: str, *, ingest: bool = True) -> str:
    """A dispatched Run with one successful check, exported to the Sink."""
    path = run_dir(root, days_old, "ok", suffix, outcomes=[{"ref": REF, "outcome": "ok"}])
    write_jsonl(
        path / "records/product.jsonl",
        [
            {
                "url": "https://shop.example/walnuts",
                "name": "Natural Walnuts 500g",
                "price": 5.49,
                "_meta": {"ref": REF, "scraped_at": (NOW - timedelta(days=days_old)).isoformat()},
            }
        ],
    )
    PostgresSink(DSN).export(SavedRun.load(path))
    if ingest:
        assert DispatchStore(DSN).ingest("shop", path.name)
    return path.name


def counts(db: psycopg.Connection[tuple[Any, ...]], run_id: str) -> dict[str, int]:
    tables = [
        "scrape_runs",
        "scrape_records",
        "scrape_outcomes",
        "dispatch.checks",
        "dispatch.price_observations",
    ]
    query = "SELECT count(*) FROM {} WHERE run_id = %s"
    return {
        table: db.execute(query.format(table), (run_id,)).fetchone()[0]  # type: ignore[index]
        for table in tables
    }


def test_sink_rows_of_ingested_runs_go_after_14_days_but_price_history_stays(
    db: psycopg.Connection[tuple[Any, ...]], tmp_path: Path
) -> None:
    old = ingested_run(tmp_path, 20, "aaaaaa")
    recent = ingested_run(tmp_path, 10, "bbbbbb")
    pending = ingested_run(tmp_path, 20, "cccccc", ingest=False)

    swept = sweep(DSN, tmp_path, NOW)

    assert swept.exported == [("shop", old)]
    assert counts(db, old) == {
        "scrape_runs": 1,
        "scrape_records": 0,
        "scrape_outcomes": 0,
        "dispatch.checks": 1,
        "dispatch.price_observations": 1,
    }
    assert counts(db, recent)["scrape_records"] == counts(db, recent)["scrape_outcomes"] == 1
    assert counts(db, pending)["scrape_records"] == counts(db, pending)["scrape_outcomes"] == 1


def test_run_directories_are_kept_14_days_or_60_when_failed_or_degraded(
    db: psycopg.Connection[tuple[Any, ...]], tmp_path: Path
) -> None:
    runs = {
        (days, health): run_dir(tmp_path, days, health, f"{days:02d}{suffix}")
        for days in (10, 20, 70)
        for health, suffix in (("ok", "a0a0"), ("degraded", "b0b0"), ("failed", "c0c0"))
    }
    unfinished = run_dir(tmp_path, 20, None, "d0d0d0")  # crashed: kept like a failed Run

    swept = sweep(DSN, tmp_path, NOW)

    deleted = {key for key, path in runs.items() if not path.exists()}
    assert deleted == {(20, "ok"), (70, "ok"), (70, "degraded"), (70, "failed")}
    assert sorted(swept.run_dirs) == sorted(runs[key] for key in deleted)
    assert unfinished.exists()


def test_manual_runs_are_never_touched(
    db: psycopg.Connection[tuple[Any, ...]], tmp_path: Path
) -> None:
    manual = run_dir(tmp_path, 70, "ok", "eeeeee", dispatched=False)
    PostgresSink(DSN).export(SavedRun.load(manual))
    replay = tmp_path / "shop" / "20260101T000000Z-ffffff"  # same Site, never dispatched
    replay.mkdir()

    assert sweep(DSN, tmp_path, NOW) == Swept()

    assert manual.exists() and replay.exists()
    assert counts(db, manual.name)["scrape_runs"] == 1


def test_a_dry_run_lists_what_it_would_delete_and_deletes_nothing(
    db: psycopg.Connection[tuple[Any, ...]], tmp_path: Path
) -> None:
    old = ingested_run(tmp_path, 20, "aaaaaa")
    before = counts(db, old)

    swept = sweep(DSN, tmp_path, NOW, dry_run=True)

    assert swept == Swept(run_dirs=[tmp_path / "shop" / old], exported=[("shop", old)])
    assert (tmp_path / "shop" / old).exists()
    assert counts(db, old) == before
    assert sweep(DSN, tmp_path, NOW) == swept  # then the real sweep deletes the same
    assert sweep(DSN, tmp_path, NOW) == Swept()


def test_scrape_janitor_reports_each_deletion_and_dry_run_says_would(
    db: psycopg.Connection[tuple[Any, ...]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = ingested_run(tmp_path / "runs", 70, "aaaaaa")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRAPE_SINK", DSN)

    dry = CliRunner().invoke(app, ["janitor", "--dry-run"])

    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines() == [
        f"Would delete Run directory runs/shop/{old}",
        f"Would delete Records and Start Request Outcomes of Run shop/{old}",
    ]
    assert (tmp_path / "runs" / "shop" / old).exists()

    real = CliRunner().invoke(app, ["janitor"])

    assert real.exit_code == 0, real.output
    assert real.stdout.splitlines() == [
        f"Deleted Run directory runs/shop/{old}",
        f"Deleted Records and Start Request Outcomes of Run shop/{old}",
    ]
    assert not (tmp_path / "runs" / "shop" / old).exists()
