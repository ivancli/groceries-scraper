"""Exports a finished Run's Records beyond its directory, e.g. to Postgres or S3."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from groceries_scraper.run.directory import RunDirectoryError, SavedRun


class ExportError(Exception):
    pass


class SinkError(ExportError):
    """A Sink was unreachable or rejected the Run, as opposed to the Run being unexportable."""


class Sink(Protocol):
    def prepare(self) -> None:
        """Fails fast, before a crawl spends time on a Run the Sink can't take."""

    def export(self, run: SavedRun) -> None:
        """Replaces anything previously exported for the same Run, so re-exports are safe."""


def open_sink(url: str) -> Sink:
    """Errors name only the scheme: sink URLs may carry credentials."""
    parsed = urlsplit(url)
    if parsed.scheme in ("postgres", "postgresql"):
        try:
            from groceries_scraper.run.sinks.postgres import PostgresSink
        except ModuleNotFoundError as exc:
            raise _missing_extra(exc, "psycopg", "postgres") from None
        return PostgresSink(url)
    if parsed.scheme == "s3":
        try:
            from groceries_scraper.run.sinks.s3 import S3Sink
        except ModuleNotFoundError as exc:
            raise _missing_extra(exc, "boto3", "s3") from None
        if not parsed.netloc:
            raise ExportError("an s3:// sink needs a bucket: s3://<bucket>[/<prefix>]")
        return S3Sink(parsed.netloc, parsed.path.strip("/"))
    raise ExportError(
        f"unsupported sink scheme `{parsed.scheme}`: use postgres://… or s3://<bucket>[/<prefix>]"
    )


def _missing_extra(exc: ModuleNotFoundError, module: str, extra: str) -> Exception:
    if exc.name != module:
        return exc
    return ExportError(f"the {extra} sink needs {module}: install groceries-scraper[{extra}]")


def export_run(path: Path, sinks: Sequence[Sink], force: bool = False) -> bool:
    """False when skipped: a failed Run must not replace good data downstream unless forced."""
    try:
        run = SavedRun.load(path)
    except RunDirectoryError as exc:
        raise ExportError(str(exc)) from exc
    if run.health is None:
        raise ExportError(f"Run {run.run_id} has not finished: run.json has no Run Health")
    if run.health == "failed" and not force:
        return False
    failures = []
    for sink in sinks:
        try:
            sink.export(run)
        except Exception as exc:  # one unreachable backend shouldn't cost the others the Run
            failures.append(f"{sink}: {exc}")
    if failures:
        raise SinkError(f"Run {run.run_id} was not exported to " + "; ".join(failures))
    return True
