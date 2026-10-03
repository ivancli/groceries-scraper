"""Stores a Run and its Records in the `scrape_runs` and `scrape_records` tables."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from groceries_scraper.run.sinks import ExportError, RunExport

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


@dataclass(frozen=True)
class PostgresSink:
    dsn: str = field(repr=False)  # may hold a password

    def __str__(self) -> str:
        url = urlsplit(self.dsn)
        port = f":{url.port}" if url.port else ""
        return f"postgres://{url.hostname or ''}{port}{url.path}"

    def publish(self, export: RunExport) -> None:
        """One transaction: readers see the Run's old rows or its new ones, never a mix."""
        psycopg, Jsonb = _driver()
        with psycopg.connect(self.dsn) as connection, connection.transaction():
            connection.execute(SCHEMA)
            connection.execute(
                "DELETE FROM scrape_runs WHERE site = %s AND run_id = %s",
                (export.site, export.run_id),
            )
            connection.execute(
                "INSERT INTO scrape_runs (site, run_id, health, manifest) VALUES (%s, %s, %s, %s)",
                (export.site, export.run_id, export.health, Jsonb(export.manifest)),
            )
            with connection.cursor().copy(
                "COPY scrape_records (site, run_id, record_type, record_key, data, meta) FROM STDIN"
            ) as copy:
                for record_type in export.record_types():
                    for row in export.records(record_type):
                        meta = row.pop("_meta", {})
                        key = export.key_values(record_type, row)
                        copy.write_row(
                            (
                                export.site,
                                export.run_id,
                                record_type,
                                Jsonb(key) if key is not None else None,
                                Jsonb(row),
                                Jsonb(meta),
                            )
                        )


def _driver() -> tuple[Any, Any]:
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError:
        raise ExportError(
            "the postgres sink needs psycopg: install groceries-scraper[postgres]"
        ) from None
    return psycopg, Jsonb
