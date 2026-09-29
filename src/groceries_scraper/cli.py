from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(no_args_is_help=True, help="Config-driven groceries scraper.")
fixture_app = typer.Typer(no_args_is_help=True, help="Manage Golden Fixtures.")
app.add_typer(fixture_app, name="fixture")


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"`{command}` is not implemented yet.", err=True)
    raise typer.Exit(code=1)


@app.command()
def validate(site_config: Path) -> None:
    """Validate a Site config."""
    _not_implemented("validate")


@app.command()
def run(
    site_config: Path,
    limit: int | None = None,
    record: Annotated[str | None, typer.Option(help="all | errors | off")] = None,
) -> None:
    """Run a Site."""
    _not_implemented("run")


@app.command()
def replay(
    run_dir: Path,
    config: Annotated[Path | None, typer.Option(help="Edited Site config.")] = None,
) -> None:
    """Replay a prior Run's Captures as a new Run."""
    _not_implemented("replay")


@app.command()
def inspect(run_dir: Path, capture_no: int) -> None:
    """Show a Capture and its Extraction Trace."""
    _not_implemented("inspect")


@fixture_app.command("save")
def fixture_save(run_dir: Path) -> None:
    """Save a Run as a Golden Fixture under tests/sites/<site>/."""
    _not_implemented("fixture save")
