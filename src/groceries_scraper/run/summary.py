"""The concise end-of-Run summary printed by the CLI."""

from collections.abc import Mapping

from groceries_scraper.run.health import RunHealth
from groceries_scraper.run.stats import RunStats


def format_summary(stats: RunStats, health: RunHealth) -> str:
    dropped = f"dropped: {stats.dropped}"
    if stats.drops_by_reason:
        dropped += f" ({_counts(stats.drops_by_reason, ': ')})"
    lines = [
        f"records: {_counts(stats.records)}",
        f"pages: {_counts(stats.pages)}",
        dropped,
        f"http: {_counts({str(k): v for k, v in sorted(stats.http_status.items())}, ': ')}",
        f"duration: {stats.duration_seconds:.1f}s",
        f"health: {health.status}",
        *(f"  {breach}" for breach in health.breaches),
    ]
    return "\n".join(lines)


def _counts(counts: Mapping[str, int], separator: str = " ") -> str:
    return ", ".join(f"{name}{separator}{n}" for name, n in counts.items()) or "none"
