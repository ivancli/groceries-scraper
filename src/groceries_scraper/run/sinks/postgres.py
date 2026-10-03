"""Stores a Run and its Records in the `scrape_runs` and `scrape_records` tables."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks import SinkError

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    site text NOT NULL,
    run_id text NOT NULL,
    health text NOT NULL,
    manifest jsonb NOT NULL,
    exported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, run_id)
);
CREATE TABLE IF NOT EXISTS scrape_records (
    site text NOT NULL,
    run_id text NOT NULL,
    record_type text NOT NULL,
    record_key jsonb,
    data jsonb NOT NULL,
    meta jsonb NOT NULL,
    FOREIGN KEY (site, run_id) REFERENCES scrape_runs ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS scrape_records_by_run ON scrape_records (site, run_id);
CREATE INDEX IF NOT EXISTS scrape_records_by_key ON scrape_records (site, record_type, record_key);
"""
SCHEMA_LOCK = 0x5C4A9E  # serialises concurrent first-time schema creation


@dataclass(frozen=True)
class PostgresSink:
    dsn: str = field(repr=False)  # may hold a password

    def __str__(self) -> str:
        url = urlsplit(self.dsn)
        return f"postgres://{url.netloc.rpartition('@')[2]}{url.path}"

    def prepare(self) -> None:
        """Creates the tables only when missing, so exports need no DDL rights or locks."""
        try:
            with psycopg.connect(self.dsn) as connection:
                tables = "SELECT to_regclass('scrape_runs'), to_regclass('scrape_records')"
                row = connection.execute(tables).fetchone()
                if row is None or None in row:
                    connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                    connection.execute(SCHEMA)
        except psycopg.Error as exc:
            raise SinkError(f"{self} is not usable: {exc}") from None

    def export(self, run: SavedRun) -> None:
        """One transaction: readers see the Run's old rows or its new ones, never a mix."""
        with psycopg.connect(self.dsn) as connection, connection.transaction():
            # Concurrent exports of one Run would otherwise collide on its primary key.
            run_lock = "SELECT pg_advisory_xact_lock(hashtext(%s))"
            connection.execute(run_lock, (f"{run.site}/{run.run_id}",))
            connection.execute(
                "DELETE FROM scrape_runs WHERE site = %s AND run_id = %s", (run.site, run.run_id)
            )
            connection.execute(
                "INSERT INTO scrape_runs (site, run_id, health, manifest) VALUES (%s, %s, %s, %s)",
                (run.site, run.run_id, run.health, Jsonb(_storable(run.manifest))),
            )
            with connection.cursor().copy(
                "COPY scrape_records (site, run_id, record_type, record_key, data, meta) FROM STDIN"
            ) as copy:
                for record_type in run.record_types():
                    key = run.keys.get(record_type)
                    for record in run.records(record_type):
                        meta = record.pop("_meta", {})
                        copy.write_row(
                            (
                                run.site,
                                run.run_id,
                                record_type,
                                Jsonb(_storable(key.values(record))) if key else None,
                                Jsonb(_storable(record)),
                                Jsonb(_storable(meta)),
                            )
                        )


def _storable(value: Any) -> Any:
    """jsonb rejects NUL, which would otherwise fail the whole Run over one scraped string."""
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, dict):
        return {_storable(name): _storable(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_storable(item) for item in value]
    return value
