"""Retention for dispatched Runs: their directories and generic Sink rows, never price history."""

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from groceries_scraper.run.directory import RUN_ID, RunDirectoryError, SavedRun

RETENTION = timedelta(days=14)
UNHEALTHY_RETENTION = timedelta(days=60)  # failed/degraded Runs are worth debugging longer


@dataclass(frozen=True)
class Swept:
    run_dirs: list[Path] = field(default_factory=list)
    sink_runs: list[tuple[str, str]] = field(default_factory=list)  # (site, run_id)


def sweep(dsn: str, runs: Path, now: datetime, *, dry_run: bool = False) -> Swept:
    """What was deleted, or with `dry_run` what would be. Runs not dispatched are never touched."""
    with psycopg.connect(dsn) as connection:
        dispatched = connection.execute(
            "SELECT site, run_id, created_at FROM dispatch.dispatches"
            " WHERE created_at < %s ORDER BY created_at, site, run_id",
            (now - RETENTION,),
        ).fetchall()
        sink_runs = _sweep_sink_rows(connection, now - RETENTION, dry_run)
    swept = Swept(sink_runs=sink_runs)
    for site, run_id, created_at in dispatched:
        path = _run_dir(runs, site, run_id)
        if path is None or not path.is_dir():
            continue
        if created_at >= now - _retention(path):
            continue
        if not dry_run:
            shutil.rmtree(path)
        swept.run_dirs.append(path)
    return swept


def _sweep_sink_rows(
    connection: psycopg.Connection[Any], before: datetime, dry_run: bool
) -> list[tuple[str, str]]:
    """`scrape_runs` stays: its manifest is small and says the Run was exported."""
    expired = """
        SELECT site, run_id FROM dispatch.dispatches d
        WHERE ingested_at IS NOT NULL AND created_at < %s AND (
            EXISTS (SELECT FROM scrape_records r WHERE (r.site, r.run_id) = (d.site, d.run_id))
            OR EXISTS (SELECT FROM scrape_outcomes o WHERE (o.site, o.run_id) = (d.site, d.run_id))
        )
        ORDER BY created_at, site, run_id
    """
    runs = [(site, run_id) for site, run_id in connection.execute(expired, (before,)).fetchall()]
    if not dry_run:
        for delete in (
            "DELETE FROM scrape_records t USING dispatch.dispatches d",
            "DELETE FROM scrape_outcomes t USING dispatch.dispatches d",
        ):
            connection.execute(
                delete + " WHERE (t.site, t.run_id) = (d.site, d.run_id)"
                " AND d.ingested_at IS NOT NULL AND d.created_at < %s",
                (before,),
            )
    return runs


def _run_dir(runs: Path, site: str, run_id: str) -> Path | None:
    """None for names that would step outside `runs`."""
    if not RUN_ID.fullmatch(run_id) or site in ("", ".", "..") or "/" in site or "\\" in site:
        return None
    return runs / site / run_id


def _retention(path: Path) -> timedelta:
    """An unreadable or unfinished Run likely crashed, so it is kept like a failed one."""
    try:
        healthy = SavedRun.load(path).health == "ok"
    except RunDirectoryError:
        healthy = False
    return RETENTION if healthy else UNHEALTHY_RETENTION
