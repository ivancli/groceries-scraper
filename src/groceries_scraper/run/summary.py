"""A finished Run's stats and Run Health, as saved to `run.json` and printed by the CLI."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from groceries_scraper.run.health import RunHealth
from groceries_scraper.run.stats import RunStats


@dataclass(frozen=True)
class RunOutcome:
    stats: RunStats
    health: RunHealth

    def to_json(self) -> dict[str, Any]:
        return {"stats": self.stats.to_json(), "health": self.health.to_json()}

    def summary(self) -> str:
        stats = self.stats.to_json()
        dropped = f"dropped: {stats['dropped']['total']}"
        if stats["dropped"]["by_reason"]:
            dropped += f" ({_counts(stats['dropped']['by_reason'], ': ')})"
        requests = f"requests: ok {stats['requests']['ok']}"
        if failures := stats["requests"]["failed"]:
            requests += f", failed {sum(failures.values())} ({_counts(failures, ': ')})"
        if missing := stats["requests"]["missing"]:
            requests += f", missing {missing}"
        return "\n".join(
            [
                f"records: {_counts(stats['records'])}",
                f"pages: {_counts(stats['pages'])}",
                dropped,
                requests,
                f"http: {_counts(stats['http_status'], ': ')}",
                *(
                    [f"sessions: {self.stats.sessions_lost} of {self.stats.sessions} lost"]
                    if self.stats.sessions_lost
                    else []
                ),
                f"duration: {self.stats.duration_seconds:.1f}s",
                f"health: {self.health.level}",
                *(f"  {breach}" for breach in self.health.breaches),
            ]
        )


def _counts(counts: Mapping[str, int], separator: str = " ") -> str:
    return ", ".join(f"{name}{separator}{n}" for name, n in counts.items()) or "none"
