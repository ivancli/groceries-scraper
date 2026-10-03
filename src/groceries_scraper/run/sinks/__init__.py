"""Publishes a finished Run's Records beyond its directory, e.g. to Postgres or S3."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from groceries_scraper.config.models import RecordType
from groceries_scraper.run.directory import read_manifest
from groceries_scraper.run.keys import RecordKey


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class RunExport:
    path: Path
    site: str
    run_id: str
    health: str
    manifest: dict[str, Any]
    keys: dict[str, RecordKey]

    @classmethod
    def load(cls, path: Path) -> "RunExport":
        if not (path / "run.json").is_file():
            raise ExportError(f"{path} is not a Run directory: no run.json")
        try:
            manifest = read_manifest(path)
            health = manifest.get("health", {}).get("level")
            keys = {
                name: RecordKey(key)
                for name, spec in manifest["config"].get("records", {}).items()
                if (key := RecordType.model_validate(spec).key)
            }
            site, run_id = manifest["site"], manifest["run_id"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ExportError(f"Cannot read Run {path}: {exc}") from exc
        if health is None:
            raise ExportError(f"Run {run_id} has not finished: run.json has no Run Health")
        return cls(path, site, run_id, health, manifest, keys)

    def record_files(self) -> list[Path]:
        return sorted((self.path / "records").glob("*.jsonl"))

    def record_types(self) -> list[str]:
        return [path.stem for path in self.record_files()]

    def records(self, record_type: str) -> Iterator[dict[str, Any]]:
        """Rows as written, `_meta` included."""
        with (self.path / "records" / f"{record_type}.jsonl").open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    yield json.loads(line)

    def key_values(self, record_type: str, row: dict[str, Any]) -> dict[str, Any] | None:
        key = self.keys.get(record_type)
        return key.values(row) if key is not None else None


class Sink(Protocol):
    def publish(self, export: RunExport) -> None:
        """Replaces anything previously published for the same Run, so re-exports are safe."""


def open_sink(url: str) -> Sink:
    """Errors name only the scheme: sink URLs may carry credentials."""
    parsed = urlsplit(url)
    if parsed.scheme in ("postgres", "postgresql"):
        from groceries_scraper.run.sinks.postgres import PostgresSink

        return PostgresSink(url)
    if parsed.scheme == "s3":
        from groceries_scraper.run.sinks.s3 import S3Sink

        if not parsed.netloc:
            raise ExportError("an s3:// sink needs a bucket: s3://<bucket>[/<prefix>]")
        return S3Sink(parsed.netloc, parsed.path.strip("/"))
    raise ExportError(
        f"unsupported sink scheme `{parsed.scheme}`: use postgres://… or s3://<bucket>[/<prefix>]"
    )


def export_run(path: Path, sinks: Sequence[Sink], force: bool = False) -> bool:
    """False when skipped: a failed Run must not replace good data downstream unless forced."""
    export = RunExport.load(path)
    if export.health == "failed" and not force:
        return False
    failures = []
    for sink in sinks:
        try:
            sink.publish(export)
        except Exception as exc:  # one unreachable backend shouldn't cost the others the Run
            failures.append(f"{sink}: {exc}")
    if failures:
        raise ExportError(f"Run {export.run_id} was not exported to " + "; ".join(failures))
    return True
