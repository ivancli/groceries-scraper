"""`run` / `replay --sink` exit codes, with the crawl and Sink backends faked."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from groceries_scraper.adapter import crawl as crawl_module
from groceries_scraper.cli import app
from groceries_scraper.config import Site
from groceries_scraper.run import Run
from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.health import HealthLevel, RunHealth
from groceries_scraper.run.sinks import SinkError
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.summary import RunOutcome

SITE = """
site: s
settings: {download_delay: 0, concurrent_requests_per_domain: 1, obey_robots: false,
           record_level: all}
start: [{url: "https://x.example/", page_type: listing}]
page_types:
  listing: {}
"""


class FakeSink:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.exported: list[str] = []

    def __str__(self) -> str:
        return "fake://sink"

    def prepare(self) -> None:
        if self.fail == "prepare":
            raise SinkError("fake://sink is not usable: down")

    def export(self, run: SavedRun) -> None:
        if self.fail == "export":
            raise ConnectionError("down")
        self.exported.append(run.run_id)


@pytest.fixture
def crawls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[HealthLevel]:
    """Push the Run Health the next fake crawl should end with."""
    monkeypatch.chdir(tmp_path)
    levels: list[HealthLevel] = []

    def crawl(site: Site, run: Run, limit: int | None = None, **_: Any) -> RunOutcome:
        level = levels.pop(0)
        manifest = {"site": run.site, "run_id": run.run_id, "config": {"site": run.site}}
        (run.path / "run.json").write_text(json.dumps({**manifest, "health": {"level": level}}))
        return RunOutcome(RunStats.for_site(site), RunHealth(level))

    monkeypatch.setattr(crawl_module, "crawl", crawl)
    (tmp_path / "site.yaml").write_text(SITE)
    return levels


def _with_sink(monkeypatch: pytest.MonkeyPatch, sink: FakeSink) -> FakeSink:
    monkeypatch.setattr("groceries_scraper.cli.open_sink", lambda url: sink)
    return sink


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["run", "site.yaml", "--sink", "fake://", *args])


def test_a_healthy_run_is_exported_and_exits_0(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _with_sink(monkeypatch, FakeSink())
    crawls.append("ok")

    result = _run()

    assert result.exit_code == 0, result.output
    [run_id] = sink.exported
    assert f"Exported Run {run_id} to fake://sink" in result.output


def test_a_sink_failing_after_a_degraded_run_exits_3(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_sink(monkeypatch, FakeSink(fail="export"))
    crawls.append("degraded")

    result = _run()

    assert result.exit_code == 3
    assert "was not exported to fake://sink: down" in result.output


def test_a_failed_run_is_not_exported_and_exits_2_with_the_command_to_force_it(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _with_sink(monkeypatch, FakeSink())
    crawls.append("failed")

    result = _run()

    assert result.exit_code == 2
    assert sink.exported == []
    [run_dir] = Path("runs/s").iterdir()
    assert f"not exported (force with `scrape export {run_dir} --force`)" in result.output


def test_an_unusable_sink_exits_3_before_the_crawl(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_sink(monkeypatch, FakeSink(fail="prepare"))

    result = _run()

    assert result.exit_code == 3
    assert "fake://sink is not usable: down" in result.output
    assert not Path("runs").exists()


def test_a_replay_is_exported_too(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = _with_sink(monkeypatch, FakeSink())
    source = tmp_path / "source"
    (source / "captures").mkdir(parents=True)
    config = yaml.safe_load(SITE)
    (source / "run.json").write_text(json.dumps({"site": "s", "run_id": "1", "config": config}))
    crawls.append("ok")

    result = CliRunner().invoke(app, ["replay", str(source), "--sink", "fake://"])

    assert result.exit_code == 0, result.output
    assert len(sink.exported) == 1
