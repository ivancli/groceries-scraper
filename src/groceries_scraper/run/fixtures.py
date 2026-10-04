"""Save Captures and stable Records for offline Site regression tests."""

import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from groceries_scraper.config.models import DEFAULT_LOCATION
from groceries_scraper.run.directory import finish_reason, read_manifest
from groceries_scraper.run.health import FINISHED


class FixtureError(Exception):
    pass


def record_data(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Ignore crawl order and volatile metadata; preserve duplicates and array order."""
    records = {}
    for path in sorted(directory.glob("*.jsonl")):
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(_stable_record(json.loads(line)))
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
        records[path.name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return records


def _stable_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("Record must be a JSON object")
    original_metadata = record.get("_meta", {})
    if not isinstance(original_metadata, dict):
        raise ValueError("Record _meta must be a JSON object")
    data = {key: value for key, value in record.items() if key != "_meta"}
    metadata = {
        key: value
        for key, value in original_metadata.items()
        if key not in {"run_id", "scraped_at", "capture_no"}
    }
    if metadata:
        # Records from before Locations scraped the implicit one.
        metadata.setdefault("location", DEFAULT_LOCATION)
        data["_meta"] = metadata
    return data


def save_fixture(run_dir: Path, root: Path = Path("tests/sites")) -> Path:
    from groceries_scraper.adapter.replay import ReplayError, SourceRun

    try:
        source = SourceRun.load(run_dir)
        manifest = read_manifest(run_dir)
        for section in ("stats", "health"):
            if not isinstance(manifest.get(section, {}), dict):
                raise FixtureError(f"{run_dir / 'run.json'}: {section} must be a JSON object")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", source.run.site):
            raise FixtureError("Fixture Site names must contain only letters, digits, _ or -")
        if finish_reason(manifest) != FINISHED:
            raise FixtureError(
                "Save a completed Run without --limit; limited Runs cannot replay fully"
            )
        if manifest.get("health", {}).get("level") != "ok":
            raise FixtureError("Save a Run with Run Health ok")
        if manifest.get("missing"):
            raise FixtureError("Cannot save a Replay with missing Captures")
        records = record_data(run_dir / "records")
        if not any(records.values()):
            raise FixtureError("Run has no Records to save")
        if not any((run_dir / "captures").glob("*.meta.json")):
            raise FixtureError("Run has no Captures to save")
        root.mkdir(parents=True, exist_ok=True)
        destination = root / source.run.site
        # Stage before replacing the prior fixture so failed reads leave it intact.
        with TemporaryDirectory(prefix=".fixture-", dir=root) as temporary:
            staging = Path(temporary) / "new"
            staging.mkdir()
            if destination.exists():
                for entry in destination.iterdir():
                    if entry.name in {"run.json", "captures", "records"}:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.copytree(entry, staging / entry.name, symlinks=True)
                    else:
                        shutil.copy2(entry, staging / entry.name, follow_symlinks=False)
            shutil.copy2(run_dir / "run.json", staging / "run.json")
            shutil.copytree(run_dir / "captures", staging / "captures")
            (staging / "records").mkdir()
            for name, rows in records.items():
                (staging / "records" / name).write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
                    ),
                    encoding="utf-8",
                )
            backup = Path(temporary) / "old"
            if destination.exists():
                destination.rename(backup)
            try:
                staging.rename(destination)
            except OSError:
                if backup.exists():
                    backup.rename(destination)
                raise
        return destination
    except (ReplayError, OSError, ValueError, KeyError, TypeError) as exc:
        raise FixtureError(str(exc)) from exc
