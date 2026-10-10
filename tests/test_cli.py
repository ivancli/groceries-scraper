import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from groceries_scraper.cli import app

ROOT = Path(__file__).parents[1]


def _design_example() -> str:
    design = (ROOT / "docs" / "design.md").read_text()
    match = re.search(r"## Site config — full example\s+```yaml\n(.*?)```", design, re.S)
    assert match
    return match.group(1)


def test_help_lists_the_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("validate", "run", "replay", "inspect", "fixture", "diff", "export", "deploy"):
        assert command in result.output


@pytest.mark.parametrize("command", ["run", "replay", "export"])
def test_command_help_renders_sink_placeholders(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output
    # Typer forces colour under GITHUB_ACTIONS, splitting the text with ANSI styles.
    assert "s3://<bucket>[/<prefix>]" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


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
    config = yaml.safe_load(_design_example())
    config["page_types"]["product_api"]["fields"]["unit_price"][1] = {
        "fn": "mypkg.transforms:parse_unit_price"
    }
    result = _validate(tmp_path, yaml.safe_dump(config))

    assert result.exit_code == 1
    assert result.stderr.splitlines()[1:] == [
        "  page_types.product_api.fields.unit_price[1]: "
        "cannot load `mypkg.transforms:parse_unit_price`: "
        "ModuleNotFoundError: No module named 'mypkg'"
    ]


def test_validate_passes_the_documented_design_example(tmp_path: Path) -> None:
    result = _validate(tmp_path, _design_example())
    assert result.exit_code == 0, result.output


def test_validate_exits_non_zero_on_a_schema_error(tmp_path: Path) -> None:
    result = _validate(tmp_path, "site: s\nstart: []\npage_types: {}\n")

    assert result.exit_code == 1
    assert "start: List should have at least 1 item" in result.stderr


def test_validate_passes_every_site_in_the_repo() -> None:
    result = CliRunner().invoke(app, ["validate", str(ROOT / "sites")])

    assert result.exit_code == 0, result.output
    assert "aldi_picks.yaml is valid" in result.stdout


ACCEPTING_SITE = """
site: {name}
schedule: {{every: 30m}}
accepts:
  page_type: product
  retailer: {{key: {name}, name: {name}}}
  url: '{pattern}'
  examples: ['{example}']
records: {{product: {{}}}}
start: [{{url: '{example}', page_type: product}}]
page_types:
  product:
    record: product
    fields:
      url: {{css: "link::attr(href)", type: string, required: true}}
      name: {{css: "h1::text", type: string, required: true}}
      price: {{css: ".price::text", type: number, required: true}}
"""


def test_validate_a_directory_reports_every_invalid_site_and_overlapping_accepts(
    tmp_path: Path,
) -> None:
    sites = {
        "a": ("^https://x\\.example/", "https://x.example/p/1"),
        "b": ("^https://x\\.example/p/", "https://x.example/p/2"),
        "c": ("^https://y\\.example/", "https://y.example/p/1"),
    }
    for name, (pattern, example) in sites.items():
        config = ACCEPTING_SITE.format(name=name, pattern=pattern, example=example)
        (tmp_path / f"{name}.yaml").write_text(config)
    (tmp_path / "broken.yaml").write_text("site: broken\nstart: []\npage_types: {}\n")

    result = CliRunner().invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert f"{tmp_path / 'c.yaml'} is valid" in result.stdout
    assert "start: List should have at least 1 item" in result.stderr
    assert (
        f"{tmp_path / 'a.yaml'}: accepts.examples[0]: `https://x.example/p/1` "
        f"is also accepted by Site `b` ({tmp_path / 'b.yaml'})"
    ) in result.stderr
    assert (
        f"{tmp_path / 'b.yaml'}: accepts.examples[0]: `https://x.example/p/2` "
        f"is also accepted by Site `a` ({tmp_path / 'a.yaml'})"
    ) in result.stderr


def test_run_rejects_an_unknown_record_level(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", str(tmp_path / "site.yaml"), "--record", "unknown"])

    assert result.exit_code == 2
    assert "must be all, errors or off" in result.stderr


@pytest.mark.parametrize("command", ["run", "replay"])
def test_an_unsupported_sink_is_rejected_before_any_run_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, [command, str(tmp_path / "x"), "--sink", "ftp://u:secret@h"])

    assert result.exit_code == 1
    assert "unsupported sink scheme `ftp`" in result.output
    assert "secret" not in result.output
    assert not (tmp_path / "runs").exists()


def test_export_needs_a_sink(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["export", str(tmp_path)])

    assert result.exit_code == 2
    assert "Missing option" in result.stderr  # Rich styles the option name on CI


def test_replay_rejects_a_directory_that_is_not_a_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["replay", str(tmp_path)])

    assert result.exit_code == 1
    assert "not a Run directory" in result.output


def test_replay_reports_a_source_config_that_no_longer_validates(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "captures").mkdir(parents=True)
    config = {"site": "s", "settings": {"record_level": "all"}, "unknown": 1}
    (run_dir / "run.json").write_text(json.dumps({"site": "s", "run_id": "r", "config": config}))

    result = CliRunner().invoke(app, ["replay", str(run_dir)])

    assert result.exit_code == 1
    assert f"invalid Site config {run_dir}" in result.output
