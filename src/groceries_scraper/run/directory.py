"""A Run's identity and output directory: `runs/<site>/<run_id>/`."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Run:
    site: str
    run_id: str
    path: Path


def create_run(root: Path, site: str, now: datetime | None = None) -> Run:
    """Run ids sort by start time; the random suffix keeps same-second Runs apart."""
    started = (now or datetime.now(UTC)).astimezone(UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    path = root / site / run_id
    path.mkdir(parents=True)
    return Run(site, run_id, path)
