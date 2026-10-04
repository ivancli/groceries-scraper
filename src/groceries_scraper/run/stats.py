"""A Run's statistics, collected during the crawl and saved to `run.json`."""

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from groceries_scraper.config import Site
from groceries_scraper.config.models import FieldSpec
from groceries_scraper.engine.extract import DroppedRecord, Record


@dataclass
class RunStats:
    pages: Counter[str] = field(default_factory=Counter)
    records: Counter[str] = field(default_factory=Counter)  # written, so within `--limit`
    extracted: int = 0  # including Records the limit cut
    dropped: int = 0
    drops_by_reason: Counter[str] = field(default_factory=Counter)
    requests_ok: int = 0
    request_failures: Counter[str] = field(default_factory=Counter)
    requests_missing: int = 0  # Replay requests no Capture matched; never sent
    http_status: Counter[int] = field(default_factory=Counter)  # every Capture
    pool_size: int = 1
    sessions_lost: int = 0
    duration_seconds: float = 0.0
    finish_reason: str | None = None
    _fields: dict[str, dict[str, FieldSpec]] = field(default_factory=dict)
    _field_values: Counter[tuple[str, str]] = field(default_factory=Counter)
    _field_nulls: Counter[tuple[str, str]] = field(default_factory=Counter)

    @classmethod
    def for_site(cls, site: Site) -> Self:
        """Declared Record Types start at zero, so missing ones still show up."""
        fields: dict[str, dict[str, FieldSpec]] = {}
        for page_type in site.page_types.values():
            if page_type.record is not None:
                fields.setdefault(page_type.record, {}).update(page_type.fields)
        return cls(
            records=Counter(dict.fromkeys(site.records, 0)),
            pool_size=site.session.pool if site.session else 1,
            _fields=fields,
        )

    def add_page(self, page_type: str) -> None:
        self.pages[page_type] += 1

    def add_extracted(self) -> None:
        self.extracted += 1

    def add_record(self, record: Record) -> None:
        self.records[record.record_type] += 1
        specs = self._fields.get(record.record_type, {})
        for path, value in _field_values(specs, record.data, ""):
            self._field_values[record.record_type, path] += 1
            self._field_nulls[record.record_type, path] += value is None

    def add_dropped(self, dropped: DroppedRecord) -> None:
        self.dropped += 1
        self.drops_by_reason.update(dropped.reason_kinds)

    def add_request_ok(self) -> None:
        self.requests_ok += 1

    def add_request_failed(self, reason: str) -> None:
        self.request_failures[reason] += 1

    def add_missing(self) -> None:
        self.requests_missing += 1

    def add_session_lost(self) -> None:
        self.sessions_lost += 1

    def add_response(self, status: int) -> None:
        self.http_status[status] += 1

    def dropped_ratio(self) -> float:
        return _ratio(self.dropped, self.dropped + self.extracted)

    def null_ratio(self) -> dict[str, dict[str, float]]:
        ratios: dict[str, dict[str, float]] = {}
        for (record_type, path), count in sorted(self._field_values.items()):
            nulls = self._field_nulls[record_type, path]
            ratios.setdefault(record_type, {})[path] = _ratio(nulls, count)
        return ratios

    def http_error_ratio(self) -> float:
        """Over page requests' final outcomes: retries and Session Setup don't count."""
        failed = self.request_failures.total()
        return _ratio(failed, failed + self.requests_ok)

    def to_json(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "finish_reason": self.finish_reason,
            "pages": dict(sorted(self.pages.items())),
            "records": dict(sorted(self.records.items())),
            "dropped": {"total": self.dropped, "by_reason": dict(self.drops_by_reason)},
            "null_ratio": self.null_ratio(),
            "requests": {
                "ok": self.requests_ok,
                "failed": dict(self.request_failures),
                "missing": self.requests_missing,
            },
            "http_status": {str(status): n for status, n in sorted(self.http_status.items())},
            "sessions": {"pool": self.pool_size, "lost": self.sessions_lost},
        }


def _field_values(
    specs: Mapping[str, FieldSpec], data: Mapping[str, Any], prefix: str
) -> Iterator[tuple[str, Any]]:
    """Nested Fields count only where their object or list element exists."""
    for name, spec in specs.items():
        if name not in data:
            continue
        path, value = f"{prefix}{name}", data[name]
        yield path, value
        if not spec.fields:
            continue
        if isinstance(value, Mapping):
            yield from _field_values(spec.fields, value, f"{path}.")
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, Mapping):
                    yield from _field_values(spec.fields, element, f"{path}[].")


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 0.0
