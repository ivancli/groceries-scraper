import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from groceries_scraper.config import ConfigError, Findings, Site, checked_site, load_checked_site
from groceries_scraper.run import create_run
from groceries_scraper.run.sinks import ExportError, Sink, export_run, open_sink
from groceries_scraper.run.summary import RunOutcome

RUNS_DIR = Path("runs")
EXPORT_FAILED = 3  # beyond Run Health's 0 / 1 / 2

SinkOption = Annotated[
    list[str] | None,
    typer.Option(
        "--sink",
        help="Export the finished Run to postgres://… or s3://<bucket>[/<prefix>]; repeatable.",
    ),
]

app = typer.Typer(no_args_is_help=True, help="Config-driven groceries scraper.")
fixture_app = typer.Typer(no_args_is_help=True, help="Manage Golden Fixtures.")
app.add_typer(fixture_app, name="fixture")


@app.command()
def validate(site_config: Path) -> None:
    """Validate a Site config."""
    _load_site_or_exit(site_config)
    typer.echo(f"{site_config} is valid")


def _load_site_or_exit(site_config: Path) -> Site:
    return _site_or_exit(lambda: load_checked_site(site_config))


def _site_or_exit(load: Callable[[], tuple[Site, Findings]]) -> Site:
    try:
        site, findings = load()
    except ConfigError as exc:
        _warn(exc.findings.warnings)
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _warn(findings.warnings)
    return site


def _open_sinks_or_exit(urls: list[str] | None) -> list[Sink]:
    try:
        return [open_sink(url) for url in urls or []]
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


def _export(run_dir: Path, sinks: list[Sink], force: bool = False) -> bool:
    """Reports the outcome; False when the Run was skipped or a sink failed."""
    try:
        exported = export_run(run_dir, sinks, force)
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        return False
    if not exported:
        typer.echo(f"Run {run_dir.name} failed: not exported (use --force)", err=True)
        return False
    typer.echo(f"Exported Run {run_dir.name} to {', '.join(map(str, sinks))}", err=True)
    return True


def _finish(outcome: RunOutcome, run_dir: Path, sinks: list[Sink]) -> NoReturn:
    typer.echo(outcome.summary())
    exported = not sinks or _export(run_dir, sinks)
    # A skipped failed Run already exits as failed; a broken sink needs its own code.
    code = EXPORT_FAILED if not exported and outcome.health.level != "failed" else None
    raise typer.Exit(code=code if code is not None else outcome.health.exit_code)


def _warn(warnings: list[str]) -> None:
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command()
def run(
    site_config: Path,
    limit: Annotated[int | None, typer.Option(min=1, help="Stop after N Records.")] = None,
    record: Annotated[str | None, typer.Option(help="all | errors | off")] = None,
    sink: SinkOption = None,
) -> None:
    """Run a Site; exits 0 / 1 / 2 for Run Health ok / degraded / failed, 3 if export failed."""
    from groceries_scraper.adapter.crawl import crawl  # Scrapy is slow to import

    sinks = _open_sinks_or_exit(sink)
    if record not in (None, "all", "errors", "off"):
        raise typer.BadParameter("must be all, errors or off", param_hint="--record")
    site = _load_site_or_exit(site_config)
    if record is not None:
        site = site.model_copy(
            update={"settings": site.settings.model_copy(update={"record_level": record})}
        )
    new_run = create_run(RUNS_DIR, site.site)
    typer.echo(f"Run {new_run.run_id}: {new_run.path}", err=True)
    _finish(crawl(site, new_run, limit), new_run.path, sinks)


@app.command()
def replay(
    run_dir: Path,
    config: Annotated[Path | None, typer.Option(help="Edited Site config.")] = None,
    sink: SinkOption = None,
) -> None:
    """Replay a prior Run's Captures offline as a new Run; exits like `run`."""
    from groceries_scraper.adapter.crawl import crawl
    from groceries_scraper.adapter.replay import ReplayError, SourceRun

    sinks = _open_sinks_or_exit(sink)
    try:
        source = SourceRun.load(run_dir)
    except ReplayError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if config is not None:
        site = _load_site_or_exit(config)
        if frozenset(site.replay.ignore_params) != source.index.ignore_params:
            _warn(["replay.ignore_params differs from the source Run's, which Replay matches by"])
    else:
        # The snapshot already holds the defaults the source Run ran with.
        site = _site_or_exit(lambda: checked_site(source.config, {}, str(run_dir)))
    new_run = create_run(RUNS_DIR, site.site)
    typer.echo(f"Run {new_run.run_id} (replay of {source.run.run_id}): {new_run.path}", err=True)
    _finish(crawl(site, new_run, replay_of=source), new_run.path, sinks)


@app.command()
def inspect(
    run_dir: Path,
    capture_no: Annotated[int, typer.Argument(min=1)],
    field: Annotated[
        str | None, typer.Option(help="Show only this Field and nested Fields.")
    ] = None,
    body: Annotated[bool, typer.Option(help="Pretty-print the response JSON/HTML body.")] = False,
) -> None:
    """Show a Capture and its Extraction Trace."""
    from rich.console import Console

    from groceries_scraper.run.inspection import inspect_capture

    try:
        inspect_capture(run_dir, capture_no, Console(highlight=False), field, body)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Cannot inspect Capture {capture_no}: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def diff(
    old_run: Path,
    new_run: Path,
    field: Annotated[str | None, typer.Option(help="Report changes to this Field only.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full diff as JSON.")] = False,
) -> None:
    """Compare two Runs of a Site: Records added, removed or changed, matched by Record Key."""
    from groceries_scraper.run.diff import DiffError, diff_runs

    try:
        result = diff_runs(old_run, new_run, only_field=field)
    except DiffError as exc:
        typer.echo(f"Cannot diff Runs: {exc}", err=True)
        raise typer.Exit(code=1) from None
    _warn(result.warnings)
    if as_json:
        typer.echo(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    else:
        typer.echo(result.summary())


@app.command()
def export(
    run_dir: Path,
    sink: Annotated[
        list[str],
        typer.Option("--sink", help="postgres://… or s3://<bucket>[/<prefix>]; repeatable."),
    ],
    force: Annotated[bool, typer.Option(help="Export even a failed Run.")] = False,
) -> None:
    """Export a finished Run's Records to Postgres or S3, replacing any earlier export of it."""
    if not _export(run_dir, _open_sinks_or_exit(sink), force):
        raise typer.Exit(code=1)


@fixture_app.command("save")
def fixture_save(run_dir: Path) -> None:
    """Save a Run as a Golden Fixture under tests/sites/<site>/."""
    from groceries_scraper.run.fixtures import FixtureError, save_fixture

    try:
        destination = save_fixture(run_dir)
    except FixtureError as exc:
        typer.echo(f"Cannot save fixture: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Saved fixture: {destination}")
