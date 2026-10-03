from pathlib import Path
from typing import Annotated, NoReturn

import typer

from groceries_scraper.config import ConfigError, Site, load_checked_site
from groceries_scraper.run import create_run

RUNS_DIR = Path("runs")

app = typer.Typer(no_args_is_help=True, help="Config-driven groceries scraper.")
fixture_app = typer.Typer(no_args_is_help=True, help="Manage Golden Fixtures.")
app.add_typer(fixture_app, name="fixture")


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"`{command}` is not implemented yet.", err=True)
    raise typer.Exit(code=1)


@app.command()
def validate(site_config: Path) -> None:
    """Validate a Site config."""
    _load_site_or_exit(site_config)
    typer.echo(f"{site_config} is valid")


def _load_site_or_exit(site_config: Path) -> Site:
    try:
        site, findings = load_checked_site(site_config)
    except ConfigError as exc:
        _warn(exc.findings.warnings)
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _warn(findings.warnings)
    return site


def _warn(warnings: list[str]) -> None:
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command()
def run(
    site_config: Path,
    limit: Annotated[int | None, typer.Option(min=1, help="Stop after N Records.")] = None,
    record: Annotated[str | None, typer.Option(help="all | errors | off")] = None,
) -> None:
    """Run a Site; exits 0 / 1 / 2 for Run Health ok / degraded / failed."""
    from groceries_scraper.adapter.crawl import crawl  # Scrapy is slow to import

    if record not in (None, "all", "errors", "off"):
        raise typer.BadParameter("must be all, errors or off", param_hint="--record")
    site = _load_site_or_exit(site_config)
    if record is not None:
        site = site.model_copy(
            update={"settings": site.settings.model_copy(update={"record_level": record})}
        )
    new_run = create_run(RUNS_DIR, site.site)
    typer.echo(f"Run {new_run.run_id}: {new_run.path}", err=True)
    outcome = crawl(site, new_run, limit)
    typer.echo(outcome.summary())
    raise typer.Exit(code=outcome.health.exit_code)


@app.command()
def replay(
    run_dir: Path,
    config: Annotated[Path | None, typer.Option(help="Edited Site config.")] = None,
) -> None:
    """Replay a prior Run's Captures as a new Run."""
    _not_implemented("replay")


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


@fixture_app.command("save")
def fixture_save(run_dir: Path) -> None:
    """Save a Run as a Golden Fixture under tests/sites/<site>/."""
    _not_implemented("fixture save")
