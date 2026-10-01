import re
from pathlib import Path

from typer.testing import CliRunner, Result

from groceries_scraper.cli import app

ROOT = Path(__file__).parents[1]


def _design_example() -> str:
    design = (ROOT / "docs" / "design.md").read_text()
    match = re.search(r"## Site config — full example\s+```yaml\n(.*?)```", design, re.S)
    assert match
    return match.group(1)


def test_help_lists_the_five_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("validate", "run", "replay", "inspect", "fixture"):
        assert command in result.output


def _validate(tmp_path: Path, config: str) -> Result:
    site_file = tmp_path / "site.yaml"
    site_file.write_text(config)
    return CliRunner().invoke(app, ["validate", str(site_file)])


def test_validate_passes_a_valid_site_and_prints_warnings(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        """
site: s
start: [{url: "https://x.example/", page_type: listing}]
page_types:
  listing: {}
  orphan: {}
""",
    )

    assert result.exit_code == 0
    assert "warning: page_types.orphan: unreachable from any Start Request" in result.stderr
    assert "site.yaml is valid" in result.stdout


def test_validate_exits_non_zero_on_a_semantic_error(tmp_path: Path) -> None:
    result = _validate(tmp_path, _design_example())

    assert result.exit_code == 1
    assert result.stderr.splitlines()[1:] == [
        "  page_types.product_api.fields.unit_price[1]: "
        "cannot import `mypkg.transforms`: No module named 'mypkg'"
    ]


def test_validate_exits_non_zero_on_a_schema_error(tmp_path: Path) -> None:
    result = _validate(tmp_path, "site: s\nstart: []\npage_types: {}\n")

    assert result.exit_code == 1
    assert "start: List should have at least 1 item" in result.stderr
