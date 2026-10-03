"""Compare two Runs of a Site by Record Key: which Records were added, removed or changed."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groceries_scraper.config.models import RecordType
from groceries_scraper.run.health import FINISHED
from groceries_scraper.run.keys import key_values


class DiffError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    key: dict[str, Any]
    fields: dict[str, tuple[Any, Any]]  # Field -> (old, new)


@dataclass(frozen=True)
class RecordTypeDiff:
    record_type: str
    key: list[str]
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    unchanged: int = 0


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
            "record_types": [
                {
                    "record_type": diff.record_type,
                    "key": diff.key,
                    "added": diff.added,
                    "removed": diff.removed,
                    "changed": [
                        {
                            "key": change.key,
                            "fields": {
                                name: {"old": old, "new": new}
                                for name, (old, new) in change.fields.items()
                            },
                        }
                        for change in diff.changed
                    ],
                    "unchanged": diff.unchanged,
                }
                for diff in self.record_types
            ],
        }

    def summary(self) -> str:
        lines = []
        for diff in self.record_types:
            lines.append(
                f"{diff.record_type} (key: {', '.join(diff.key)}): {len(diff.added)} added, "
                f"{len(diff.removed)} removed, {len(diff.changed)} changed, "
                f"{diff.unchanged} unchanged"
            )
            lines += [f"  + {_json(_key(diff.key, record))}" for record in diff.added]
            lines += [f"  - {_json(_key(diff.key, record))}" for record in diff.removed]
            lines += [
                f"  ~ {_json(change.key)} "
                + "; ".join(
                    f"{name}: {_json(old)} -> {_json(new)}"
                    for name, (old, new) in change.fields.items()
                )
                for change in diff.changed
            ]
        return "\n".join(lines)


def _key(key: list[str], record: dict[str, Any]) -> dict[str, Any]:
    return {name: record.get(name) for name in key}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class _Run:
    path: Path
    site: str
    run_id: str
    keys: dict[str, list[str]]
    finish_reason: str | None

    @classmethod
    def load(cls, path: Path) -> "_Run":
        if not (path / "run.json").is_file():
            raise DiffError(f"{path} is not a Run directory: no run.json")
        manifest = json.loads((path / "run.json").read_text(encoding="utf-8"))
        records = manifest["config"].get("records", {})
        keys = {name: RecordType.model_validate(spec).key for name, spec in records.items()}
        finish_reason = manifest.get("stats", {}).get("finish_reason")
        return cls(path, manifest["site"], manifest["run_id"], keys, finish_reason)

    def records(self, record_type: str, key: list[str]) -> dict[str, dict[str, Any]]:
        """By key values; Runs from before deduplication keep their first Record per key."""
        path = self.path / "records" / f"{record_type}.jsonl"
        if not path.is_file():
            return {}
        by_key: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = {name: value for name, value in json.loads(line).items() if name != "_meta"}
            if all(data.get(name) is not None for name in key):
                by_key.setdefault(key_values(key, data), data)
        return by_key


def diff_runs(old_path: Path, new_path: Path, field: str | None = None) -> RunDiff:
    """`field` limits changes to that Field; added and removed Records are always reported."""
    old, new = _load(old_path), _load(new_path)
    if old.site != new.site:
        raise DiffError(f"Runs are of different Sites: {old.site} and {new.site}")
    record_types = []
    for record_type, key in sorted(new.keys.items()):
        old_key = old.keys.get(record_type, [])
        if key and old_key and old_key != key:
            raise DiffError(
                f"Record Key of `{record_type}` differs: {_names(old_key)} and {_names(key)}"
            )
        if key:
            record_types.append(_diff(record_type, key, old, new, field))
    if not record_types:
        raise DiffError("no Record Type declares a Record Key, so Records cannot be matched")
    warnings = [
        f"Run {run.run_id} did not finish ({run.finish_reason}): "
        "Records it missed show as added or removed"
        for run in (old, new)
        if run.finish_reason != FINISHED
    ]
    return RunDiff(new.site, old.run_id, new.run_id, record_types, warnings)


def _names(key: list[str]) -> str:
    return f"[{', '.join(key)}]"


def _load(path: Path) -> _Run:
    try:
        return _Run.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DiffError(f"Cannot read Run {path}: {exc}") from exc


def _diff(
    record_type: str, key: list[str], old: _Run, new: _Run, field: str | None
) -> RecordTypeDiff:
    before, after = old.records(record_type, key), new.records(record_type, key)
    changed, unchanged = [], 0
    for k in sorted(before.keys() & after.keys()):
        names = [field] if field is not None else sorted(before[k].keys() | after[k].keys())
        fields = {
            name: (before[k].get(name), after[k].get(name))
            for name in names
            if before[k].get(name) != after[k].get(name)
        }
        if fields:
            changed.append(Change(json.loads(k), fields))
        else:
            unchanged += 1
    return RecordTypeDiff(
        record_type,
        key,
        added=[after[k] for k in sorted(after.keys() - before.keys())],
        removed=[before[k] for k in sorted(before.keys() - after.keys())],
        changed=changed,
        unchanged=unchanged,
    )
