"""Mirrors a Run's `run.json` and `records/*.jsonl` to `s3://<bucket>/<prefix>/<site>/<run_id>/`."""

from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks import SinkError

DELETE_BATCH = 1000  # the DeleteObjects limit


@dataclass(frozen=True)
class S3Sink:
    bucket: str
    prefix: str

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def prepare(self) -> None:
        try:
            boto3.client("s3").head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise SinkError(f"{self} is not usable: {exc}") from None

    def export(self, run: SavedRun) -> None:
        """`run.json` goes last, so readers can treat it as the Run's completion marker."""
        client = boto3.client("s3")
        base = "/".join(part for part in (self.prefix, run.site, run.run_id) if part)
        uploads = {
            f"{base}/records/{record_type}.jsonl": run.path / "records" / f"{record_type}.jsonl"
            for record_type in run.record_types()
        }
        # Includes the old run.json, so a half-replaced Run never looks complete.
        stale = [
            {"Key": entry["Key"]}
            for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=f"{base}/"
            )
            for entry in page.get("Contents", [])
            if entry["Key"] not in uploads
        ]
        for start in range(0, len(stale), DELETE_BATCH):
            batch = {"Objects": stale[start : start + DELETE_BATCH]}
            # DeleteObjects reports per-key failures instead of raising.
            if errors := client.delete_objects(Bucket=self.bucket, Delete=batch).get("Errors"):
                failed = ", ".join(f"{error['Key']}: {error['Code']}" for error in errors)
                raise SinkError(f"could not delete {failed}")
        for key, path in uploads.items():
            client.upload_file(str(path), self.bucket, key)
        client.upload_file(str(run.path / "run.json"), self.bucket, f"{base}/run.json")
