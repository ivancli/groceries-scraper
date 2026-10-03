"""Record Keys within a Run: the first Record with a key is kept, later ones are dropped."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from groceries_scraper.config.models import RecordType
from groceries_scraper.engine.extract import DroppedRecord, ExtractionResult, Record

DUPLICATE_KEY = "duplicate Record Key"


@dataclass(frozen=True)
class RecordKey:
    fields: list[str]

    def __str__(self) -> str:
        return ", ".join(self.fields)

    def values(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return {name: data.get(name) for name in self.fields}

    def identity(self, data: Mapping[str, Any]) -> str:
        """Canonical JSON, so equal structured values match whatever their order."""
        return json.dumps(self.values(data), sort_keys=True, ensure_ascii=False)

    def missing(self, data: Mapping[str, Any]) -> str | None:
        return next((name for name in self.fields if data.get(name) is None), None)


class RecordKeys:
    def __init__(self, records: Mapping[str, RecordType]) -> None:
        self._keys = {name: RecordKey(spec.key) for name, spec in records.items() if spec.key}
        self._seen: dict[tuple[str, str], int] = {}  # -> first Capture

    def filter(self, extraction: ExtractionResult, capture_no: int) -> ExtractionResult:
        """Moves rejected Records to `dropped`, in Scope order with the extraction's drops."""
        records, dropped = [], list(extraction.dropped)
        for record in extraction.records:
            if rejection := self.admit(record, capture_no):
                dropped.append(rejection)
            else:
                records.append(record)
        dropped.sort(key=lambda entry: entry.index)
        return replace(extraction, records=records, dropped=dropped)

    def admit(self, record: Record, capture_no: int) -> DroppedRecord | None:
        key = self._keys.get(record.record_type)
        if key is None:
            return None
        if (name := key.missing(record.data)) is not None:
            reason = f"Record Key Field `{name}` is missing"
            return DroppedRecord(record.index, reason, (reason,))
        identity = key.identity(record.data)
        first = self._seen.get((record.record_type, identity))
        if first is None:
            self._seen[record.record_type, identity] = capture_no
            return None
        reason = f"{DUPLICATE_KEY} {identity} (first in Capture {first})"
        return DroppedRecord(record.index, reason, (DUPLICATE_KEY,))
