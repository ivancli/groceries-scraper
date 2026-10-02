"""Session Setup: one step's response → Session Variables. Descriptions only, never I/O."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from groceries_scraper.config.models import SetupStep
from groceries_scraper.engine.extract import FieldTrace
from groceries_scraper.engine.page import parse_scope
from groceries_scraper.engine.pipe import PipeContext, plain, run_pipe


@dataclass(frozen=True)
class SetupResult:
    session: dict[str, Any] = field(default_factory=dict)  # empty when `error` is set
    error: str | None = None
    trace: list[FieldTrace] = field(default_factory=list)


def evaluate_setup(
    step: SetupStep, status: int, body: bytes, content_type: str | None, ctx: PipeContext
) -> SetupResult:
    """Setup has no `response:` key, so the content type picks the Scope."""
    if not 200 <= status < 300:
        return SetupResult(error=f"Session Setup request got HTTP {status}")
    is_json = "json" in (content_type or "").lower()
    try:
        scope = parse_scope("json" if is_json else "html", body, content_type)
    except ValueError as exc:
        return SetupResult(error=f"Session Setup response is not JSON: {exc}")
    session = {}
    trace = []
    for name, pipe in step.extract.items():
        values, steps = run_pipe(pipe, scope, ctx)
        trace.append(FieldTrace(f"session.{name}", 0, steps))
        if not values:
            # Later templates would fail on every request; stop the Run here instead.
            cause = next((f": {s.error}" for s in steps if s.error), "")
            return SetupResult(error=f"Session Variable `{name}` has no value{cause}", trace=trace)
        session[name] = plain(values[0])
    return SetupResult(session, trace=trace)


class RefreshAction(Enum):
    PROCEED = "proceed"
    REFRESH = "refresh"  # then retry
    WAIT = "wait"  # Setup is already re-running: retry once it ends
    RETRY = "retry"  # sent with an older Session: retry with the current one
    GIVE_UP = "give_up"


class SessionRefresh:
    """`generation` tags a request's Session, so failures from one stale Session refresh once."""

    def __init__(self, refresh_on: Iterable[int], max_refresh: int) -> None:
        self.refresh_on = frozenset(refresh_on)
        self.max_refresh = max_refresh
        self.generation = 0
        self.refreshes = 0
        self.refreshing = False

    def on_status(self, status: int, generation: int) -> RefreshAction:
        if status not in self.refresh_on:
            return RefreshAction.PROCEED
        if self.refreshing:
            return RefreshAction.WAIT
        if generation < self.generation:
            return RefreshAction.RETRY
        if self.refreshes >= self.max_refresh:
            return RefreshAction.GIVE_UP
        self.refreshes += 1
        self.refreshing = True
        return RefreshAction.REFRESH

    def refreshed(self) -> None:
        self.generation += 1
        self.refreshing = False
