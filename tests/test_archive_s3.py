from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sink_runs import saved_run

from groceries_scraper.run.archive import ArchiveError, ArchiveUrlError, open_archive
from groceries_scraper.run.directory import SavedRun

PREFIX = "archive/shop/melb/20260101T000000Z-abc123"


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


def _run_with_captures(tmp_path: Path) -> Path:
    run = saved_run(tmp_path, {"product": [{"sku": "a"}]}, location="melb")
    (run / "captures").mkdir()
    (run / "captures/0001-listing.meta.json").write_text('{"status": 200}')
    (run / "captures/0001-listing.body").write_bytes(b"\x1f\x8b\x00\xffraw")
    (run / "traces").mkdir()
    (run / "traces/0001-listing.trace.json").write_text("{}")
    return run


def test_the_whole_run_directory_is_mirrored_byte_for_byte(tmp_path: Path, s3: Any) -> None:
    run = _run_with_captures(tmp_path)
    archive = open_archive("s3://bucket/archive/")
    archive.prepare()

    url = archive.archive(SavedRun.load(run))

    assert url == f"s3://bucket/{PREFIX}/"
    assert _objects(s3) == {
        f"{PREFIX}/{relative}": (run / relative).read_bytes()
        for relative in (
            "run.json",
            "records/product.jsonl",
            "captures/0001-listing.meta.json",
            "captures/0001-listing.body",
            "traces/0001-listing.trace.json",
        )
    }


def test_a_run_recorded_without_captures_archives_only_what_it_wrote(
    tmp_path: Path, s3: Any
) -> None:
    run = saved_run(tmp_path, {"product": [{"sku": "a"}]})

    open_archive("s3://bucket").archive(SavedRun.load(run))

    assert sorted(_objects(s3)) == [
        "shop/default/20260101T000000Z-abc123/records/product.jsonl",
        "shop/default/20260101T000000Z-abc123/run.json",
    ]


def test_run_json_is_uploaded_last(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run_with_captures(tmp_path)
    uploaded: list[str] = []

    class RecordingClient:
        def __getattr__(self, name: str) -> Any:
            return getattr(s3, name)

        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploaded.append(key.rsplit("/", 1)[-1])
            s3.upload_file(filename, bucket, key)

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: RecordingClient())

    open_archive("s3://bucket/archive").archive(SavedRun.load(run))

    assert len(uploaded) == 5
    assert uploaded[-1] == "run.json"


def test_a_missing_bucket_is_not_usable(s3: Any) -> None:
    with pytest.raises(ArchiveError, match="s3://missing/archive is not usable"):
        open_archive("s3://missing/archive").prepare()


def test_an_upload_failure_is_an_archive_error(tmp_path: Path, s3: Any) -> None:
    run = saved_run(tmp_path, {"product": [{"sku": "a"}]})

    with pytest.raises(ArchiveError, match="Run 20260101T000000Z-abc123 was not archived"):
        open_archive("s3://missing").archive(SavedRun.load(run))


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("postgres://user:secret@host/db", "unsupported archive scheme `postgres`"),
        ("s3:///prefix", "needs a bucket"),
    ],
)
def test_an_archive_url_must_name_an_s3_bucket(url: str, message: str) -> None:
    with pytest.raises(ArchiveUrlError, match=message) as raised:
        open_archive(url)

    assert "secret" not in str(raised.value)
