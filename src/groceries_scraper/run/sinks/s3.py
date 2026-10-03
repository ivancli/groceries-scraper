"""Mirrors a Run's `run.json` and `records/*.jsonl` to `s3://<bucket>/<prefix>/<site>/<run_id>/`."""

from dataclasses import dataclass
from typing import Any

from groceries_scraper.run.sinks import ExportError, RunExport


@dataclass(frozen=True)
class S3Sink:
    bucket: str
    prefix: str

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")

    def publish(self, export: RunExport) -> None:
        """`run.json` goes last, so readers can treat it as the Run's completion marker."""
        client = _client()
        base = "/".join(part for part in (self.prefix, export.site, export.run_id) if part)
        uploads = {f"{base}/records/{path.name}": path for path in export.record_files()}
        stale = [
            {"Key": entry["Key"]}
            for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=f"{base}/"
            )
            for entry in page.get("Contents", [])
            if entry["Key"] not in uploads
        ]
        # Delete the old run.json first, so a half-replaced Run never looks complete.
        for start in range(0, len(stale), 1000):  # the DeleteObjects limit
            client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": stale[start : start + 1000]}
            )
        for key, path in uploads.items():
            client.upload_file(str(path), self.bucket, key)
        client.upload_file(str(export.path / "run.json"), self.bucket, f"{base}/run.json")


def _client() -> Any:
    try:
        import boto3
    except ImportError:
        raise ExportError("the s3 sink needs boto3: install groceries-scraper[s3]") from None
    return boto3.client("s3")
