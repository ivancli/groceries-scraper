"""Mirrors a whole Run directory to `s3://<bucket>/<prefix>/<site>/<location>/<run_id>/`."""

from dataclasses import dataclass

import boto3
from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError

from groceries_scraper.run.archive import ArchiveError
from groceries_scraper.run.directory import SavedRun


@dataclass(frozen=True)
class S3Archive:
    bucket: str
    prefix: str

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def prepare(self) -> None:
        try:
            boto3.client("s3").head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise ArchiveError(f"{self} is not usable: {exc}") from None

    def archive(self, run: SavedRun) -> str:
        """`run.json` goes last, as in the S3 Sink, so it marks a complete Archive."""
        parts = (self.prefix, run.site, run.location, run.run_id)
        base = "/".join(part for part in parts if part)
        files = sorted(
            path.relative_to(run.path).as_posix()
            for path in run.path.rglob("*")
            if path.is_file() and path != run.path / "run.json"
        )
        try:
            client = boto3.client("s3")
            for relative in [*files, "run.json"]:
                client.upload_file(str(run.path / relative), self.bucket, f"{base}/{relative}")
        except (BotoCoreError, ClientError, Boto3Error, OSError) as exc:
            raise ArchiveError(f"Run {run.run_id} was not archived to {self}: {exc}") from None
        return f"s3://{self.bucket}/{base}/"
