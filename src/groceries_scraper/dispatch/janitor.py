"""Retention for dispatched Runs: their directories and exported Records, never price history."""

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
    # (site, run_id) of Runs whose exported Records and Start Request Outcomes went
    exported: list[tuple[str, str]] = field(default_factory=list)


def sweep(dsn: str, runs_dir: Path, now: datetime, *, dry_run: bool = False) -> Swept:
    """Runs the Dispatcher didn't create are never touched."""
    with psycopg.connect(dsn) as connection:
        dispatched = connection.execute(
            "SELECT site, run_id, created_at FROM dispatch.dispatches"
            " WHERE created_at < %s ORDER BY created_at, site, run_id",
            (now - RETENTION,),
        ).fetchall()
        exported = _sweep_exported(connection, now - RETENTION, dry_run)
    swept = Swept(exported=exported)
    for site, run_id, created_at in dispatched:
        path = _run_dir(runs_dir, site, run_id)
        if path is None or not path.is_dir():
            continue
        if created_at >= now - _retention(path):
            continue
        if not dry_run:
            shutil.rmtree(path)
        swept.run_dirs.append(path)
    return swept


EXPIRED = """
    WITH expired AS (
        SELECT site, run_id FROM dispatch.dispatches
        WHERE ingested_at IS NOT NULL AND created_at < %s
    )
"""
LIST_EXPIRED = (
    EXPIRED
    + """
    SELECT site, run_id FROM expired e WHERE
        EXISTS (SELECT FROM scrape_records r WHERE (r.site, r.run_id) = (e.site, e.run_id))
        OR EXISTS (SELECT FROM scrape_outcomes o WHERE (o.site, o.run_id) = (e.site, e.run_id))
    ORDER BY site, run_id
"""
)
# One statement, so a Run ingested meanwhile is either deleted and listed or neither.
DELETE_EXPIRED = (
    EXPIRED
    + """,
    records AS (
        DELETE FROM scrape_records t USING expired e
        WHERE (t.site, t.run_id) = (e.site, e.run_id) RETURNING t.site, t.run_id
    ),
    outcomes AS (
        DELETE FROM scrape_outcomes t USING expired e
        WHERE (t.site, t.run_id) = (e.site, e.run_id) RETURNING t.site, t.run_id
    )
    SELECT site, run_id FROM records UNION SELECT site, run_id FROM outcomes
    ORDER BY site, run_id
"""
)


def _sweep_exported(
    connection: psycopg.Connection[Any], before: datetime, dry_run: bool
) -> list[tuple[str, str]]:
    """`scrape_runs` stays: its manifest is small and says the Run was exported."""
    query = LIST_EXPIRED if dry_run else DELETE_EXPIRED
    return [(site, run_id) for site, run_id in connection.execute(query, (before,)).fetchall()]


def _run_dir(runs_dir: Path, site: str, run_id: str) -> Path | None:
    """None for names that would step outside `runs_dir`."""
    if not RUN_ID.fullmatch(run_id) or site in ("", ".", "..") or "/" in site or "\\" in site:
        return None
    return runs_dir / site / run_id


def _retention(path: Path) -> timedelta:
    """An unreadable or unfinished Run likely crashed, so it is kept like a failed one."""
    try:
        healthy = SavedRun.load(path).health == "ok"
    except RunDirectoryError:
        healthy = False
    return RETENTION if healthy else UNHEALTHY_RETENTION
