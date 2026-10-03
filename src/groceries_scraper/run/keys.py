"""Record Keys within a Run: the first Record with a key is kept, later ones are dropped."""

import json
from collections.abc import Mapping
from typing import Any, NamedTuple

from groceries_scraper.config.models import RecordType
from groceries_scraper.engine.extract import Record


class KeyRejection(NamedTuple):
    reason: str
    kind: str  # the reason without key values, for Run stats


def key_values(key: list[str], data: Mapping[str, Any]) -> str:
    """Canonical JSON, so equal structured values match whatever their order."""
    return json.dumps({name: data.get(name) for name in key}, sort_keys=True, ensure_ascii=False)


class RecordKeys:
    def __init__(self, records: Mapping[str, RecordType]) -> None:
        self._keys = {name: spec.key for name, spec in records.items() if spec.key}
        self._seen: dict[tuple[str, str], int] = {}  # -> first Capture

    def admit(self, record: Record, capture_no: int) -> KeyRejection | None:
        key = self._keys.get(record.record_type)
        if key is None:
            return None
        for name in key:
            if record.data.get(name) is None:
                reason = f"Record Key Field `{name}` is missing"
                return KeyRejection(reason, reason)
        values = key_values(key, record.data)
        first = self._seen.get((record.record_type, values))
        if first is not None:
            return KeyRejection(
                f"duplicate Record Key {values} (first in Capture {first})", "duplicate Record Key"
            )
        self._seen[record.record_type, values] = capture_no
        return None
