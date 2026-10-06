import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from groceries_scraper.config import (
    ConfigError,
    Findings,
    Site,
    check_sites,
    checked_site,
    load_checked_site,
)
from groceries_scraper.run import create_run
from groceries_scraper.run.archive import (
    Archive,
    ArchiveError,
    ArchiveUrlError,
    open_archive,
    overlapping_sinks,
)
from groceries_scraper.run.directory import RunDirectoryError, SavedRun
from groceries_scraper.run.sinks import ExportError, Sink, SinkError, export_run, open_sink
from groceries_scraper.run.summary import RunOutcome
from groceries_scraper.run.supply import Supply, SupplyError

RUNS_DIR = Path("runs")
EXPORT_FAILED = 3  # beyond Run Health's 0 / 1 / 2
ARCHIVE_FAILED = 4

SINK_HELP = "Export the finished Run to postgres://… or s3://<bucket>[/<prefix>]; repeatable."

SinkOption = Annotated[list[str] | None, typer.Option("--sink", help=SINK_HELP)]

# Lets a cluster's Secret choose every Job's Sinks; `deploy job` must not copy the shell's.
RunSinkOption = Annotated[
    list[str] | None, typer.Option("--sink", envvar="SCRAPE_SINK", help=SINK_HELP)
]

LocationOption = Annotated[
    str | None,
    typer.Option(help="The Location to scrape; required when the Site declares any."),
]

ArchiveOption = Annotated[
    str | None,
    typer.Option(
        "--archive",
        help="Copy the whole Run directory to s3://<bucket>[/<prefix>], whatever its health.",
    ),
]

app = typer.Typer(no_args_is_help=True, help="Config-driven groceries scraper.")
fixture_app = typer.Typer(no_args_is_help=True, help="Manage Golden Fixtures.")
app.add_typer(fixture_app, name="fixture")
deploy_app = typer.Typer(no_args_is_help=True, help="Render Kubernetes Jobs.")
app.add_typer(deploy_app, name="deploy")


@deploy_app.command("job")
def deploy_job(
    site_config: Path,
    location: LocationOption = None,
    sink: SinkOption = None,
    archive_url: ArchiveOption = None,
) -> None:
    """Read one Job template on stdin and write the Job for this Run on stdout."""
    from groceries_scraper.deploy import DeployError, render_job

    site = _load_site_or_exit(site_config)
    location_name = _location_or_exit(site, location)
    _refuse_overlap_or_exit(archive_url, sink)
    try:
        rendered = render_job(
            typer.get_text_stream("stdin").read(),
            site,
            site_config,
            location_name,
            sink or [],
            archive_url,
        )
    except DeployError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(rendered, nl=False)


@app.command()
def validate(site_config: Path) -> None:
    """Validate a Site config, or every `*.yaml` in a directory and their Accepts Rules together."""
    if not site_config.is_dir():
        _load_site_or_exit(site_config)
        typer.echo(f"{site_config} is valid")
        return
    sites: dict[str, Site] = {}
    failed = False
    for path in sorted(site_config.glob("*.yaml")):
        if (site := _reported_site(partial(load_checked_site, path))) is None:
            failed = True
            continue
        sites[str(path)] = site
        typer.echo(f"{path} is valid")
    if errors := check_sites(sites):
        typer.echo("\n".join(["Sites accept the same URL:", *errors]), err=True)
    if failed or errors:
        raise typer.Exit(code=1)


def _load_site_or_exit(site_config: Path) -> Site:
    return _site_or_exit(lambda: load_checked_site(site_config))


def _site_or_exit(load: Callable[[], tuple[Site, Findings]]) -> Site:
    if (site := _reported_site(load)) is None:
        raise typer.Exit(code=1)
    return site


def _reported_site(load: Callable[[], tuple[Site, Findings]]) -> Site | None:
    """Prints the findings, so a directory can report every invalid Site."""
    try:
        site, findings = load()
    except ConfigError as exc:
        _warn(exc.findings.warnings)
        typer.echo(str(exc), err=True)
        return None
    _warn(findings.warnings)
    return site


def _location_or_exit(site: Site, location: str | None) -> str:
    try:
        return site.resolve_location(location)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


def _supply_or_exit(site: Site, path: Path) -> Supply:
    try:
        supply = Supply.read(path)
    except SupplyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _check_supply_or_exit(site, supply)
    return supply


def _check_supply_or_exit(site: Site, supply: Supply) -> None:
    try:
        supply.check(site)
    except SupplyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


def _refuse_overlap_or_exit(archive_url: str | None, sink_urls: list[str] | None) -> None:
    if archive_url is None:
        return
    if overlapping := overlapping_sinks(archive_url, sink_urls or []):
        typer.echo(f"--archive {archive_url} overlaps --sink {overlapping[0]}", err=True)
        raise typer.Exit(code=1)


def _open_archive_or_exit(url: str | None) -> Archive | None:
    if url is None:
        return None
    try:
        archive = open_archive(url)
        archive.prepare()
    except ArchiveUrlError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except ArchiveError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=ARCHIVE_FAILED) from None
    return archive


def _archive(run_dir: Path, archive: Archive) -> bool:
    try:
        url = archive.archive(SavedRun.load(run_dir))
    except (ArchiveError, RunDirectoryError) as exc:
        typer.echo(str(exc), err=True)
        return False
    typer.echo(f"Archived Run {run_dir.name} to {url}", err=True)
    return True


def _open_sinks_or_exit(urls: list[str] | None) -> list[Sink]:
    try:
        sinks = [open_sink(url) for url in urls or []]
        for sink in sinks:
            sink.prepare()
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXPORT_FAILED if isinstance(exc, SinkError) else 1) from None
    return sinks


def _export(run_dir: Path, sinks: list[Sink], force: bool = False) -> int | None:
    """The exit code when the Run was not exported."""
    try:
        if export_run(run_dir, sinks, force):
            typer.echo(f"Exported Run {run_dir.name} to {', '.join(map(str, sinks))}", err=True)
            return None
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        return EXPORT_FAILED if isinstance(exc, SinkError) else 1
    force_hint = f"force with `scrape export {run_dir} --force`"
    typer.echo(f"Run {run_dir.name} failed: not exported ({force_hint})", err=True)
    return 1


def _report_and_exit(
    outcome: RunOutcome, run_dir: Path, sinks: list[Sink], archive: Archive | None
) -> NoReturn:
    typer.echo(outcome.summary())
    # Before the Sinks, so a Run stopped by SIGTERM is archived within the grace period.
    archive_failed = archive is not None and not _archive(run_dir, archive)
    # Run Health's code can't say a Sink failed; a skipped failed Run keeps its own.
    sink_failed = bool(sinks) and _export(run_dir, sinks) == EXPORT_FAILED
    if sink_failed:
        raise typer.Exit(code=EXPORT_FAILED)
    raise typer.Exit(code=ARCHIVE_FAILED if archive_failed else outcome.health.exit_code)


def _warn(warnings: list[str]) -> None:
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command()
def run(
    site_config: Path,
    limit: Annotated[int | None, typer.Option(min=1, help="Stop after N Records.")] = None,
    record: Annotated[str | None, typer.Option(help="all | errors | off")] = None,
    location: LocationOption = None,
    supply: Annotated[
        Path | None,
        typer.Option(
            help="JSONL of {ref, url} Supplied Start Requests, run instead of `start:`; "
            "writes outcomes.jsonl."
        ),
    ] = None,
    sink: RunSinkOption = None,
    archive_url: ArchiveOption = None,
) -> None:
    """Run a Site; exits 0 / 1 / 2 for Run Health, 3 if export failed, 4 if archiving failed."""
    from groceries_scraper.adapter.crawl import crawl  # Scrapy is slow to import

    if supply is not None and limit is not None:
        # A limit-cut ref would read as never checked; supply fewer refs instead.
        typer.echo("--limit cannot be used with --supply", err=True)
        raise typer.Exit(code=1)
    _refuse_overlap_or_exit(archive_url, sink)
    sinks = _open_sinks_or_exit(sink)
    archive = _open_archive_or_exit(archive_url)
    if record not in (None, "all", "errors", "off"):
        raise typer.BadParameter("must be all, errors or off", param_hint="--record")
    site = _load_site_or_exit(site_config)
    if record is not None:
        site = site.model_copy(
            update={"settings": site.settings.model_copy(update={"record_level": record})}
        )
    location = _location_or_exit(site, location)
    supplied = None if supply is None else _supply_or_exit(site, supply)
    new_run = create_run(RUNS_DIR, site.site, location=location)
    typer.echo(f"Run {new_run.run_id}: {new_run.path}", err=True)
    outcome = crawl(site, new_run, limit, supply=supplied)
    _report_and_exit(outcome, new_run.path, sinks, archive)


@app.command()
def replay(
    run_dir: Path,
    config: Annotated[Path | None, typer.Option(help="Edited Site config.")] = None,
    sink: RunSinkOption = None,
    archive_url: ArchiveOption = None,
) -> None:
    """Replay a prior Run's Captures offline as a new Run; exits like `run`."""
    from groceries_scraper.adapter.crawl import crawl
    from groceries_scraper.adapter.replay import ReplayError, SourceRun

    _refuse_overlap_or_exit(archive_url, sink)
    sinks = _open_sinks_or_exit(sink)
    archive = _open_archive_or_exit(archive_url)
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
    # The source Run's Captures answer for its Location only.
    location = _location_or_exit(site, source.run.location)
    if source.supply is not None:  # an edited config must still accept it
        _check_supply_or_exit(site, source.supply)
    new_run = create_run(RUNS_DIR, site.site, location=location)
    typer.echo(f"Run {new_run.run_id} (replay of {source.run.run_id}): {new_run.path}", err=True)
    outcome = crawl(site, new_run, replay_of=source, supply=source.supply)
    _report_and_exit(outcome, new_run.path, sinks, archive)


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
    """Export a finished Run to Postgres or S3; exits 1 if skipped/unreadable, 3 if a Sink fails."""
    if code := _export(run_dir, _open_sinks_or_exit(sink), force):
        raise typer.Exit(code=code)


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
