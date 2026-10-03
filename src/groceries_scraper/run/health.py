"""Run Health: built-in failure conditions plus the Site's Health Checks."""

from dataclasses import dataclass, field
from typing import Literal

from groceries_scraper.config import Site
from groceries_scraper.run.stats import RunStats

Status = Literal["ok", "degraded", "failed"]
_EXIT_CODES: dict[Status, int] = {"ok": 0, "degraded": 1, "failed": 2}


@dataclass(frozen=True)
class RunHealth:
    status: Status
    breaches: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.status]


def assess_health(site: Site, stats: RunStats) -> RunHealth:
    failures = ["Session Setup failed"] if stats.session_setup_failed else []
    failures += [
        f"no Records of Record Type `{record_type}`"
        for record_type in site.records
        if not stats.records[record_type]
    ]
    checks = site.health
    breaches = [
        f"min_records.{record_type}: {stats.records[record_type]} < {minimum}"
        for record_type, minimum in checks.min_records.items()
        if stats.records[record_type] < minimum
    ]
    if checks.max_dropped_ratio is not None:
        breaches += _above("max_dropped_ratio", stats.dropped_ratio(), checks.max_dropped_ratio)
    null_ratio = stats.null_ratio()
    for name, maximum in checks.max_null_ratio.items():
        breaches += _above(f"max_null_ratio.{name}", null_ratio.get(name, 0.0), maximum)
    if checks.max_http_error_ratio is not None:
        breaches += _above(
            "max_http_error_ratio", stats.http_error_ratio(), checks.max_http_error_ratio
        )
    status: Status = "failed" if failures else "degraded" if breaches else "ok"
    return RunHealth(status, failures + breaches)


def _above(check: str, value: float, maximum: float) -> list[str]:
    return [f"{check}: {value:.3f} > {maximum:.3f}"] if value > maximum else []
