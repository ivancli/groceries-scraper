"""Writes Records to `records/<record_type>.jsonl` in the Run directory."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Self

from scrapy.crawler import Crawler
from scrapy.exceptions import DropItem

from groceries_scraper.adapter.settings import RECORD_LIMIT, RECORDER, RUN
from groceries_scraper.engine.extract import Record
from groceries_scraper.run import Run
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.supply import SupplyOutcomes


@dataclass(frozen=True)
class EmittedRecord:
    """What the spider yields to Scrapy: a Record plus where it came from."""

    record: Record
    source_url: str
    capture_no: int
    ref: str | None = None  # the Supplied Start Request it came from
    scraped_at: str | None = None  # None: now


class RecordPipeline:
    def __init__(
        self, run: Run, limit: int | None, stats: RunStats, outcomes: SupplyOutcomes | None
    ) -> None:
        self.run = run
        self.limit = limit
        self.stats = stats
        self.outcomes = outcomes
        self.written = 0
        self._files: dict[str, IO[str]] = {}

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        recorder = crawler.settings[RECORDER]
        return cls(
            crawler.settings[RUN], crawler.settings[RECORD_LIMIT], recorder.stats, recorder.outcomes
        )

    def process_item(self, item: object) -> object:
        if not isinstance(item, EmittedRecord):
            return item
        if self.limit is not None and self.written >= self.limit:
            raise DropItem("Record limit reached")
        record = item.record
        meta = {
            "site": self.run.site,
            "run_id": self.run.run_id,
            "location": self.run.location,
            "record_type": record.record_type,
            "scraped_at": item.scraped_at or datetime.now(UTC).isoformat(),
            "source_url": item.source_url,
            "capture_no": item.capture_no,
        }
        if item.ref is not None:
            meta["ref"] = item.ref
        line = json.dumps({**record.data, "_meta": meta}, ensure_ascii=False)
        self._file(record.record_type).write(line + "\n")
        self.written += 1
        self.stats.add_record(record)
        if self.outcomes is not None and item.ref is not None:
            self.outcomes.add_record(item.ref)
        return item

    def _file(self, record_type: str) -> IO[str]:
        if record_type not in self._files:
            path = self.run.path / "records" / f"{record_type}.jsonl"
            path.parent.mkdir(exist_ok=True)
            self._files[record_type] = path.open("a", encoding="utf-8")
        return self._files[record_type]

    def close_spider(self) -> None:
        for file in self._files.values():
            file.close()
