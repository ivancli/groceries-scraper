"""A Run's identity and output directory: `runs/<site>/<run_id>/`."""

import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from groceries_scraper.config.models import DEFAULT_LOCATION, RecordType
from groceries_scraper.run.health import HealthLevel
from groceries_scraper.run.keys import RecordKey


@dataclass(frozen=True)
class Run:
    site: str
    run_id: str
    path: Path
    location: str = DEFAULT_LOCATION


def create_run(
    root: Path, site: str, now: datetime | None = None, *, location: str = DEFAULT_LOCATION
) -> Run:
    """Run ids sort by start time; the random suffix keeps same-second Runs apart."""
    started = (now or datetime.now(UTC)).astimezone(UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    path = root / site / run_id
    path.mkdir(parents=True)
    return Run(site, run_id, path, location)


def read_manifest(path: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((path / "run.json").read_text(encoding="utf-8"))
    return manifest


def finish_reason(manifest: dict[str, Any]) -> str | None:
    reason: str | None = manifest.get("stats", {}).get("finish_reason")
    return reason


class RunDirectoryError(Exception):
    pass


@dataclass(frozen=True)
class SavedRun:
    """A Run read back from its directory."""

    path: Path
    site: str
    run_id: str
    manifest: dict[str, Any]
    keys: dict[str, RecordKey]

    @property
    def location(self) -> str:
        """Runs from before Locations scraped the implicit one."""
        location: str = self.manifest.get("location", DEFAULT_LOCATION)
        return location

    @classmethod
    def load(cls, path: Path) -> "SavedRun":
        if not (path / "run.json").is_file():
            raise RunDirectoryError(f"{path} is not a Run directory: no run.json")
        try:
            manifest = read_manifest(path)
            keys = {
                name: RecordKey(key)
                for name, spec in manifest["config"].get("records", {}).items()
                if (key := RecordType.model_validate(spec).key)
            }
            return cls(path, manifest["site"], manifest["run_id"], manifest, keys)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RunDirectoryError(f"Cannot read Run {path}: {exc}") from exc

    @property
    def finish_reason(self) -> str | None:
        return finish_reason(self.manifest)

    @property
    def health(self) -> HealthLevel | None:
        """None until the Run has finished."""
        level: HealthLevel | None = self.manifest.get("health", {}).get("level")
        return level

    def record_types(self) -> list[str]:
        return sorted(path.stem for path in (self.path / "records").glob("*.jsonl"))

    def records(self, record_type: str) -> Iterator[dict[str, Any]]:
        """As written, `_meta` included; none when the Run wrote no such file."""
        path = self.path / "records" / f"{record_type}.jsonl"
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    yield json.loads(line)
