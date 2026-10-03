"""Run Health: built-in failure conditions plus the Site's Health Checks."""

from dataclasses import dataclass, field
from typing import Any, Literal

from groceries_scraper.config import Site
from groceries_scraper.run.stats import RunStats

# Scrapy close reasons.
FINISHED = "finished"
LIMIT_REACHED = "closespider_itemcount"
SESSION_SETUP_FAILED = "session_setup_failed"

HealthLevel = Literal["ok", "degraded", "failed"]
_EXIT_CODES: dict[HealthLevel, int] = {"ok": 0, "degraded": 1, "failed": 2}


@dataclass(frozen=True)
class Breach:
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


@dataclass(frozen=True)
class RunHealth:
    level: HealthLevel
    breaches: list[Breach] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.level]

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "breaches": [{"check": b.check, "detail": b.detail} for b in self.breaches],
        }


def assess_health(site: Site, stats: RunStats) -> RunHealth:
    # A Run stopped by `--limit` is short by design: Record counts say nothing about the Site.
    complete = stats.finish_reason != LIMIT_REACHED
    failures = _closed_early(stats.finish_reason)
    if complete:
        failures += [
            Breach(f"records.{record_type}", "no Records")
            for record_type in site.records
            if not stats.records[record_type]
        ]
    checks = site.health
    breaches = [
        Breach(f"min_records.{record_type}", f"{stats.records[record_type]} < {minimum}")
        for record_type, minimum in checks.min_records.items()
        if complete and stats.records[record_type] < minimum
    ]
    ratios = [("max_dropped_ratio", "", stats.dropped_ratio(), checks.max_dropped_ratio)]
    ratios += [
        (f"max_null_ratio.{name}", f" in `{record_type}`", by_field[name], maximum)
        for name, maximum in checks.max_null_ratio.items()
        for record_type, by_field in stats.null_ratio().items()
        if name in by_field
    ]
    ratios.append(
        ("max_http_error_ratio", "", stats.http_error_ratio(), checks.max_http_error_ratio)
    )
    breaches += [
        Breach(check, f"{value:.3f} > {maximum:.3f}{where}")
        for check, where, value, maximum in ratios
        if maximum is not None and value > maximum
    ]
    level: HealthLevel = "failed" if failures else "degraded" if breaches else "ok"
    return RunHealth(level, failures + breaches)


def _closed_early(reason: str | None) -> list[Breach]:
    if reason == SESSION_SETUP_FAILED:
        return [Breach("finish_reason", "Session Setup failed")]
    if reason in (FINISHED, LIMIT_REACHED):
        return []
    return [Breach("finish_reason", f"closed early ({reason or 'unknown'})")]
