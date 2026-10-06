from collections.abc import Iterator
from typing import Any

import pytest
from fake_tracker import FakeTracker, serve_tracker


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A moto S3 client with an empty `bucket`."""
    import boto3  # the optional s3 extra; only the S3 tests need it
    from moto import mock_aws

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


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTracker]:
    yield from serve_tracker(monkeypatch)
