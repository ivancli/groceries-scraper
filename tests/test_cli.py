from typer.testing import CliRunner

from groceries_scraper.cli import app


def test_help_lists_the_five_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("validate", "run", "replay", "inspect", "fixture"):
        assert command in result.output
