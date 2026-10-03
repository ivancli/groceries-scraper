"""A Run's statistics, collected during the crawl and saved to `run.json`."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Self

from groceries_scraper.config import Site
from groceries_scraper.engine.extract import DroppedRecord, Record


@dataclass
class RunStats:
    pages: Counter[str] = field(default_factory=Counter)
    records: Counter[str] = field(default_factory=Counter)
    dropped: int = 0
    drops_by_reason: Counter[str] = field(default_factory=Counter)
    http_status: Counter[int] = field(default_factory=Counter)
    duration_seconds: float = 0.0
    session_setup_failed: bool = False
    _field_values: Counter[str] = field(default_factory=Counter)
    _field_nulls: Counter[str] = field(default_factory=Counter)

    @classmethod
    def for_site(cls, site: Site) -> Self:
        """Declared Record Types start at zero, so missing ones still show up."""
        return cls(records=Counter(dict.fromkeys(site.records, 0)))

    def add_page(self, page_type: str) -> None:
        self.pages[page_type] += 1

    def add_record(self, record: Record) -> None:
        self.records[record.record_type] += 1
        for name, value in record.data.items():
            self._field_values[name] += 1
            self._field_nulls[name] += value is None

    def add_dropped(self, dropped: DroppedRecord) -> None:
        self.dropped += 1
        self.drops_by_reason.update(dropped.causes)

    def add_response(self, status: int) -> None:
        self.http_status[status] += 1

    def dropped_ratio(self) -> float:
        return _ratio(self.dropped, self.dropped + self.records.total())

    def null_ratio(self) -> dict[str, float]:
        """Per top-level Field name, across every Record Type that has it."""
        return {
            name: _ratio(self._field_nulls[name], count)
            for name, count in sorted(self._field_values.items())
        }

    def http_error_ratio(self) -> float:
        errors = sum(count for status, count in self.http_status.items() if status >= 400)
        return _ratio(errors, self.http_status.total())

    def to_json(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "pages": dict(sorted(self.pages.items())),
            "records": dict(sorted(self.records.items())),
            "dropped": {"total": self.dropped, "by_reason": dict(self.drops_by_reason)},
            "null_ratio": self.null_ratio(),
            "http_status": {str(status): n for status, n in sorted(self.http_status.items())},
        }


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 0.0
