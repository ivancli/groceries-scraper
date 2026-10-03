from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sink_runs import saved_run as _run
from typer.testing import CliRunner

from groceries_scraper.cli import app
from groceries_scraper.run.sinks import RunExport, export_run, open_sink

PREFIX = "scrapes/shop/20260101T000000Z-abc123"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="bucket")
        yield client


def _objects(s3: Any) -> dict[str, bytes]:
    listing = s3.list_objects_v2(Bucket="bucket").get("Contents", [])
    return {
        entry["Key"]: s3.get_object(Bucket="bucket", Key=entry["Key"])["Body"].read()
        for entry in listing
    }


def test_a_run_manifest_and_record_files_are_mirrored_under_prefix_site_and_run_id(
    tmp_path: Path, s3: Any
) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}], "promotion": [{"label": "x"}]})

    assert export_run(run, [open_sink("s3://bucket/scrapes/")])

    assert _objects(s3) == {
        f"{PREFIX}/run.json": (run / "run.json").read_bytes(),
        f"{PREFIX}/records/product.jsonl": (run / "records/product.jsonl").read_bytes(),
        f"{PREFIX}/records/promotion.jsonl": (run / "records/promotion.jsonl").read_bytes(),
    }


def test_re_exporting_a_run_removes_objects_it_no_longer_has(tmp_path: Path, s3: Any) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}], "promotion": [{"label": "x"}]})
    sink = open_sink("s3://bucket/scrapes")
    sink.publish(RunExport.load(run))
    (run / "records/promotion.jsonl").unlink()
    s3.put_object(Bucket="bucket", Key="scrapes/shop/other-run/run.json", Body=b"{}")

    sink.publish(RunExport.load(run))

    assert sorted(_objects(s3)) == [
        f"{PREFIX}/records/product.jsonl",
        f"{PREFIX}/run.json",
        "scrapes/shop/other-run/run.json",
    ]


def test_scrape_export_publishes_a_run_to_each_sink(tmp_path: Path, s3: Any) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}]})

    result = CliRunner().invoke(
        app, ["export", str(run), "--sink", "s3://bucket/a", "--sink", "s3://bucket/b"]
    )

    assert result.exit_code == 0, result.output
    assert "Exported Run 20260101T000000Z-abc123 to s3://bucket/a, s3://bucket/b" in result.output
    assert {key.split("/")[0] for key in _objects(s3)} == {"a", "b"}


def test_scrape_export_skips_a_failed_run_unless_forced(tmp_path: Path, s3: Any) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}]}, health="failed")

    skipped = CliRunner().invoke(app, ["export", str(run), "--sink", "s3://bucket"])

    assert skipped.exit_code == 1
    assert "Run 20260101T000000Z-abc123 failed: not exported (use --force)" in skipped.output
    assert _objects(s3) == {}

    forced = CliRunner().invoke(app, ["export", str(run), "--sink", "s3://bucket", "--force"])

    assert forced.exit_code == 0, forced.output
    assert len(_objects(s3)) == 2


def test_scrape_export_reports_a_sink_failure(tmp_path: Path, s3: Any) -> None:
    run = _run(tmp_path, {"product": [{"sku": "a"}]})

    result = CliRunner().invoke(app, ["export", str(run), "--sink", "s3://missing-bucket"])

    assert result.exit_code == 1
    assert "was not exported to s3://missing-bucket:" in result.output
