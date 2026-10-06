import sys
from pathlib import Path
from typing import Any

import pytest
from sink_runs import saved_run as _run

from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks import ExportError, SinkError, export_run, open_sink
from groceries_scraper.run.sinks.postgres import PostgresSink
from groceries_scraper.run.sinks.s3 import S3Sink


class FakeSink:
    def __init__(self) -> None:
        self.exported: list[dict[str, Any]] = []

    def prepare(self) -> None:
        pass

    def export(self, run: SavedRun) -> None:
        self.exported.append(
            {
                "run": (run.site, run.run_id, run.health),
                "records": {
                    record_type: [
                        (record, key.values(record) if (key := run.keys.get(record_type)) else None)
                        for record in run.records(record_type)
                    ]
                    for record_type in run.record_types()
                },
            }
        )


def test_a_finished_run_is_exported_to_every_sink_with_its_records_and_keys(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        {"product": [{"sku": "a", "price": 1.0}], "promotion": [{"label": "2 for 1"}]},
    )
    sinks = [FakeSink(), FakeSink()]

    assert export_run(run, sinks) is True

    expected = {
        "run": ("shop", "20260101T000000Z-abc123", "ok"),
        "records": {
            "product": [
                (
                    {"sku": "a", "price": 1.0, "_meta": {"run_id": "20260101T000000Z-abc123"}},
                    {"sku": "a"},
                )
            ],
            "promotion": [
                ({"label": "2 for 1", "_meta": {"run_id": "20260101T000000Z-abc123"}}, None)
            ],
        },
    }
    assert [sink.exported for sink in sinks] == [[expected], [expected]]


def test_a_failed_run_is_not_exported_unless_forced(tmp_path: Path) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}]}, health="failed")
    sink = FakeSink()

    assert export_run(run, [sink]) is False
    assert sink.exported == []

    assert export_run(run, [sink], force=True) is True
    assert [exported["run"] for exported in sink.exported] == [
        ("shop", "20260101T000000Z-abc123", "failed")
    ]


def test_a_degraded_run_is_exported(tmp_path: Path) -> None:
    sink = FakeSink()

    assert export_run(_run(tmp_path, {}, health="degraded"), [sink]) is True
    assert sink.exported == [
        {"run": ("shop", "20260101T000000Z-abc123", "degraded"), "records": {}}
    ]


def test_a_failed_supplied_run_is_exported_without_force(tmp_path: Path) -> None:
    run = _run(tmp_path, {}, health="failed", outcomes=[{"ref": "a", "outcome": "blocked"}])
    sink = FakeSink()

    assert export_run(run, [sink]) is True
    assert sink.exported == [{"run": ("shop", "20260101T000000Z-abc123", "failed"), "records": {}}]


@pytest.mark.parametrize("outcomes", [None, [{"ref": "a", "outcome": "skipped"}]])
def test_an_unfinished_run_cannot_be_exported_even_when_forced(
    tmp_path: Path, outcomes: list[dict[str, Any]] | None
) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}]}, health=None, outcomes=outcomes)

    with pytest.raises(ExportError, match="has not finished"):
        export_run(run, [FakeSink()], force=True)


def test_a_directory_without_run_json_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="not a Run directory"):
        export_run(tmp_path, [FakeSink()])


@pytest.mark.parametrize(
    ("url", "bucket", "prefix"),
    [
        ("s3://bucket", "bucket", ""),
        ("s3://bucket/", "bucket", ""),
        ("s3://bucket/scrapes/prod/", "bucket", "scrapes/prod"),
    ],
)
def test_an_s3_url_opens_an_s3_sink_under_its_prefix(url: str, bucket: str, prefix: str) -> None:
    sink = open_sink(url)

    assert isinstance(sink, S3Sink)
    assert (sink.bucket, sink.prefix) == (bucket, prefix)


@pytest.mark.parametrize("url", ["postgres://u:p@db/scrapes", "postgresql://db/scrapes"])
def test_a_postgres_url_opens_a_postgres_sink(url: str) -> None:
    sink = open_sink(url)

    assert isinstance(sink, PostgresSink)
    assert sink.dsn == url


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://user:secret@host/x", "unsupported sink scheme `ftp`"),
        ("runs/out", "unsupported sink scheme ``"),
        ("s3:///prefix", "needs a bucket"),
    ],
)
def test_an_unsupported_or_incomplete_sink_url_is_rejected(url: str, message: str) -> None:
    with pytest.raises(ExportError, match=message) as error:
        open_sink(url)
    assert "secret" not in str(error.value)


class BrokenSink:
    def prepare(self) -> None:
        pass

    def export(self, run: SavedRun) -> None:
        raise ConnectionError("connection refused")

    def __str__(self) -> str:
        return "broken://sink"


def test_a_failing_sink_does_not_stop_the_others_and_is_named_in_the_error(
    tmp_path: Path,
) -> None:
    sink = FakeSink()

    with pytest.raises(SinkError, match="broken://sink: connection refused"):
        export_run(_run(tmp_path, {"product": [{"sku": "a"}]}), [BrokenSink(), sink])
    assert len(sink.exported) == 1


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("postgres://user:secret@db:5433/scrapes?sslmode=require", "postgres://db:5433/scrapes"),
        ("postgres://user:secret@db:bad/scrapes", "postgres://db:bad/scrapes"),
        ("s3://bucket/scrapes/", "s3://bucket/scrapes"),
    ],
)
def test_sinks_are_named_without_credentials(url: str, name: str) -> None:
    sink = open_sink(url)

    assert str(sink) == name
    assert "secret" not in repr(sink)


@pytest.mark.parametrize(
    ("url", "module", "extra"),
    [("postgres://db/scrapes", "psycopg", "postgres"), ("s3://bucket", "boto3", "s3")],
)
def test_a_sink_without_its_driver_names_the_extra_to_install(
    monkeypatch: pytest.MonkeyPatch, url: str, module: str, extra: str
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    monkeypatch.delitem(sys.modules, f"groceries_scraper.run.sinks.{extra}")

    with pytest.raises(ExportError, match=rf"install groceries-scraper\[{extra}\]"):
        open_sink(url)


def test_an_unreachable_postgres_sink_fails_to_prepare_without_leaking_its_password() -> None:
    sink = open_sink("postgres://user:secret@127.0.0.1:1/scrapes")

    with pytest.raises(SinkError, match="postgres://127.0.0.1:1/scrapes is not usable:") as error:
        sink.prepare()
    assert "secret" not in str(error.value)
