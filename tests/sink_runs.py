"""Builds finished Run directories for Sink and Archive tests."""

import json
from pathlib import Path
from typing import Any

from groceries_scraper.run.supply import OUTCOMES_FILE, write_jsonl

KEYED = {"product": {"key": ["sku"]}, "promotion": {}}


def saved_run(
    root: Path,
    records: dict[str, list[dict[str, Any]]],
    *,
    health: str | None = "ok",
    run_id: str = "20260101T000000Z-abc123",
    location: str | None = None,  # None: a Run from before Locations
    outcomes: list[dict[str, Any]] | None = None,
) -> Path:
    path = root / "shop" / run_id
    (path / "records").mkdir(parents=True)
    manifest: dict[str, Any] = {
        "site": "shop",
        "run_id": run_id,
        "config": {"site": "shop", "records": KEYED},
    }
    if location is not None:
        manifest["location"] = location
    if health is not None:
        manifest["health"] = {"level": health, "breaches": []}
    if outcomes is not None:
        manifest.update(supplied=True, supplied_refs=len(outcomes))
        write_jsonl(path / OUTCOMES_FILE, outcomes)
    (path / "run.json").write_text(json.dumps(manifest))
    for record_type, typed in records.items():
        lines = [json.dumps({**record, "_meta": {"run_id": run_id}}) for record in typed]
        (path / "records" / f"{record_type}.jsonl").write_text("".join(f"{x}\n" for x in lines))
    return path


def s3_objects(s3: Any) -> dict[str, bytes]:
    listing = s3.list_objects_v2(Bucket="bucket").get("Contents", [])
    return {
        entry["Key"]: s3.get_object(Bucket="bucket", Key=entry["Key"])["Body"].read()
        for entry in listing
    }
