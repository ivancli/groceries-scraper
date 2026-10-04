"""Compare two Runs of a Site by Record Key: which Records were added, removed or changed."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groceries_scraper.run.directory import RunDirectoryError, SavedRun
from groceries_scraper.run.health import FINISHED
from groceries_scraper.run.keys import RecordKey

_Records = dict[str, dict[str, Any]]  # by Record Key identity


class DiffError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    key: dict[str, Any]
    fields: dict[str, tuple[Any, Any]]  # (old, new)

    def to_json(self) -> dict[str, Any]:
        fields = {name: {"old": old, "new": new} for name, (old, new) in self.fields.items()}
        return {"key": self.key, "fields": fields}

    def __str__(self) -> str:
        fields = (
            f"{name}: {_json(old)} -> {_json(new)}" for name, (old, new) in self.fields.items()
        )
        return f"{_json(self.key)} {'; '.join(fields)}"


@dataclass(frozen=True)
class RecordTypeDiff:
    record_type: str
    key: list[str]
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    unchanged: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "key": self.key,
            "added": self.added,
            "removed": self.removed,
            "changed": [change.to_json() for change in self.changed],
            "unchanged": self.unchanged,
        }

    def lines(self) -> list[str]:
        key = RecordKey(self.key)
        return [
            f"{self.record_type} (key: {key}): {len(self.added)} added, "
            f"{len(self.removed)} removed, {len(self.changed)} changed, {self.unchanged} unchanged",
            *(f"  + {_json(key.values(record))}" for record in self.added),
            *(f"  - {_json(key.values(record))}" for record in self.removed),
            *(f"  ~ {change}" for change in self.changed),
        ]


@dataclass(frozen=True)
class RunDiff:
    site: str
    old: str
    new: str
    record_types: list[RecordTypeDiff]
    warnings: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "old": self.old,
            "new": self.new,
            "warnings": self.warnings,
            "record_types": [diff.to_json() for diff in self.record_types],
        }

    def summary(self) -> str:
        return "\n".join(line for diff in self.record_types for line in diff.lines())


def _load(path: Path) -> SavedRun:
    try:
        return SavedRun.load(path)
    except RunDirectoryError as exc:
        raise DiffError(str(exc)) from exc


def _keyed(run: SavedRun, record_type: str, key: RecordKey) -> _Records:
    """Runs from before deduplication keep their first Record per key."""
    records: _Records = {}
    for record in run.records(record_type):
        data = {name: value for name, value in record.items() if name != "_meta"}
        if key.missing(data) is None:
            records.setdefault(key.identity(data), data)
    return records


def diff_runs(old_path: Path, new_path: Path, only_field: str | None = None) -> RunDiff:
    """`only_field` limits changes to that Field; added and removed Records are always listed."""
    old, new = _load(old_path), _load(new_path)
    if old.site != new.site:
        raise DiffError(f"Runs are of different Sites: {old.site} and {new.site}")
    if old.location != new.location:
        # The same Record Key in two Locations is two different facts.
        raise DiffError(f"Runs are of different Locations: {old.location} and {new.location}")
    record_types, field_seen = [], False
    for record_type in sorted(old.keys.keys() | new.keys.keys()):
        old_key, new_key = old.keys.get(record_type), new.keys.get(record_type)
        if old_key and new_key and old_key != new_key:
            raise DiffError(f"Record Key of `{record_type}` differs: [{old_key}] and [{new_key}]")
        # A Record Type keyed in only one Run is still compared, so its Records aren't lost.
        key = new_key or old_key
        assert key is not None
        before, after = _keyed(old, record_type, key), _keyed(new, record_type, key)
        field_seen = field_seen or any(
            only_field in data for data in [*before.values(), *after.values()]
        )
        record_types.append(_diff(record_type, key, before, after, only_field))
    if not record_types:
        raise DiffError("no Record Type declares a Record Key, so Records cannot be matched")
    if only_field is not None and not field_seen:
        # Otherwise a misspelt Field reads as "nothing changed".
        raise DiffError(f"no Record in either Run has Field `{only_field}`")
    warnings = [
        f"Run {run.run_id} did not finish ({run.finish_reason}): "
        "Records it missed show as added or removed"
        for run in (old, new)
        if run.finish_reason != FINISHED
    ]
    return RunDiff(new.site, old.run_id, new.run_id, record_types, warnings)


def _diff(
    record_type: str, key: RecordKey, before: _Records, after: _Records, only_field: str | None
) -> RecordTypeDiff:
    changed, unchanged = [], 0
    for identity in sorted(before.keys() & after.keys()):
        old, new = before[identity], after[identity]
        names = [only_field] if only_field is not None else sorted(old.keys() | new.keys())
        fields = {
            name: (old.get(name), new.get(name)) for name in names if old.get(name) != new.get(name)
        }
        if fields:
            changed.append(Change(key.values(new), fields))
        else:
            unchanged += 1
    return RecordTypeDiff(
        record_type,
        key.fields,
        added=[after[identity] for identity in sorted(after.keys() - before.keys())],
        removed=[before[identity] for identity in sorted(before.keys() - after.keys())],
        changed=changed,
        unchanged=unchanged,
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
