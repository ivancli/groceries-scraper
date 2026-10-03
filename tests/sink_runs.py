"""Builds finished Run directories for sink tests."""

import json
from pathlib import Path
from typing import Any

KEYED = {"product": {"key": ["sku"]}, "promotion": {}}


def saved_run(
    root: Path,
    records: dict[str, list[dict[str, Any]]],
    *,
    health: str | None = "ok",
    run_id: str = "20260101T000000Z-abc123",
) -> Path:
    path = root / "shop" / run_id
    (path / "records").mkdir(parents=True)
    manifest: dict[str, Any] = {
        "site": "shop",
        "run_id": run_id,
        "config": {"site": "shop", "records": KEYED},
    }
    if health is not None:
        manifest["health"] = {"level": health, "breaches": []}
    (path / "run.json").write_text(json.dumps(manifest))
    for record_type, typed in records.items():
        lines = [json.dumps({**record, "_meta": {"run_id": run_id}}) for record in typed]
        (path / "records" / f"{record_type}.jsonl").write_text("".join(f"{x}\n" for x in lines))
    return path
