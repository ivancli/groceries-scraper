"""`run` / `replay` with `--sink` and `--archive`: exit codes and ordering, backends faked."""

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
from groceries_scraper.run.archive import ArchiveError
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
    def __init__(self, fail: str | None = None, log: list[str] | None = None) -> None:
        self.fail = fail
        self.exported: list[str] = []
        self.log = log if log is not None else []

    def __str__(self) -> str:
        return "fake://sink"

    def prepare(self) -> None:
        if self.fail == "prepare":
            raise SinkError("fake://sink is not usable: down")

    def export(self, run: SavedRun) -> None:
        if self.fail == "export":
            raise ConnectionError("down")
        self.exported.append(run.run_id)
        self.log.append("export")


class FakeArchive:
    def __init__(self, fail: str | None = None, log: list[str] | None = None) -> None:
        self.fail = fail
        self.archived: list[str] = []
        self.log = log if log is not None else []

    def prepare(self) -> None:
        if self.fail == "prepare":
            raise ArchiveError("s3://archive is not usable: down")

    def archive(self, run: SavedRun) -> str:
        if self.fail == "archive":
            raise ArchiveError(f"Run {run.run_id} was not archived to s3://archive: down")
        self.archived.append(run.run_id)
        self.log.append("archive")
        return f"s3://archive/{run.site}/{run.location}/{run.run_id}/"


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


def _with_archive(monkeypatch: pytest.MonkeyPatch, archive: FakeArchive) -> FakeArchive:
    monkeypatch.setattr("groceries_scraper.cli.open_archive", lambda url: archive)
    return archive


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


def _archive_run(*args: str) -> Result:
    return CliRunner().invoke(app, ["run", "site.yaml", "--archive", "s3://archive", *args])


@pytest.mark.parametrize("level", ["ok", "failed"])
def test_a_run_is_archived_whatever_its_health_and_before_it_is_exported(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, level: HealthLevel
) -> None:
    log: list[str] = []
    archive = _with_archive(monkeypatch, FakeArchive(log=log))
    _with_sink(monkeypatch, FakeSink(log=log))
    crawls.append(level)

    result = _archive_run("--sink", "fake://")

    assert result.exit_code == (0 if level == "ok" else 2), result.output
    [run_id] = archive.archived
    assert f"Archived Run {run_id} to s3://archive/s/default/{run_id}/" in result.output
    assert log == (["archive", "export"] if level == "ok" else ["archive"])


def test_an_unusable_archive_exits_4_before_the_crawl(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_archive(monkeypatch, FakeArchive(fail="prepare"))

    result = _archive_run()

    assert result.exit_code == 4
    assert "s3://archive is not usable: down" in result.output
    assert not Path("runs").exists()


def test_an_archive_url_that_is_not_s3_exits_1_before_the_crawl(
    crawls: list[HealthLevel],
) -> None:
    result = CliRunner().invoke(app, ["run", "site.yaml", "--archive", "gs://bucket"])

    assert result.exit_code == 1
    assert "unsupported archive scheme `gs`" in result.output
    assert not Path("runs").exists()


@pytest.mark.parametrize("level", ["ok", "degraded", "failed"])
def test_an_archive_failure_exits_4_over_the_run_health(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, level: HealthLevel
) -> None:
    _with_archive(monkeypatch, FakeArchive(fail="archive"))
    sink = _with_sink(monkeypatch, FakeSink())
    crawls.append(level)

    result = _archive_run("--sink", "fake://")

    assert result.exit_code == 4
    assert "was not archived to s3://archive: down" in result.output
    assert len(sink.exported) == (0 if level == "failed" else 1)


def test_a_sink_failure_exits_3_over_an_archive_failure(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_archive(monkeypatch, FakeArchive(fail="archive"))
    _with_sink(monkeypatch, FakeSink(fail="export"))
    crawls.append("ok")

    result = _archive_run("--sink", "fake://")

    assert result.exit_code == 3


@pytest.mark.parametrize(
    ("archive", "sink"),
    [
        ("s3://bucket", "s3://bucket/records"),
        ("s3://bucket/runs/archive", "s3://bucket/runs"),
        ("s3://bucket/runs/", "s3://bucket/runs"),
    ],
)
def test_an_archive_overlapping_an_s3_sink_is_refused_before_anything_runs(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, archive: str, sink: str
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("groceries_scraper.cli.open_sink", lambda url: opened.append(url))
    monkeypatch.setattr("groceries_scraper.cli.open_archive", lambda url: opened.append(url))

    result = CliRunner().invoke(app, ["run", "site.yaml", "--archive", archive, "--sink", sink])

    assert result.exit_code == 1
    assert f"--archive {archive} overlaps --sink {sink}" in result.output
    assert opened == []
    assert not Path("runs").exists()


@pytest.mark.parametrize(
    "sink", ["s3://other/archive", "s3://bucket/archive-records", "postgres://bucket/archive"]
)
def test_an_archive_beside_a_sink_is_allowed(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, sink: str
) -> None:
    _with_archive(monkeypatch, FakeArchive())
    _with_sink(monkeypatch, FakeSink())
    crawls.append("ok")

    result = CliRunner().invoke(
        app, ["run", "site.yaml", "--archive", "s3://bucket/archive", "--sink", sink]
    )

    assert result.exit_code == 0, result.output


def test_a_replay_is_archived_too(
    crawls: list[HealthLevel], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = _with_archive(monkeypatch, FakeArchive())
    source = tmp_path / "source"
    (source / "captures").mkdir(parents=True)
    config = yaml.safe_load(SITE)
    (source / "run.json").write_text(json.dumps({"site": "s", "run_id": "1", "config": config}))
    crawls.append("ok")

    result = CliRunner().invoke(app, ["replay", str(source), "--archive", "s3://archive"])

    assert result.exit_code == 0, result.output
    assert len(archive.archived) == 1
