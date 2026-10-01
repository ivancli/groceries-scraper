"""One response through a Page Type: the Loop runs once, shared by extraction and Follow Rules."""

from dataclasses import dataclass
from typing import Any

from groceries_scraper.config.models import PageType
from groceries_scraper.engine.extract import ExtractionResult, extract
from groceries_scraper.engine.follow import FollowResult, follow
from groceries_scraper.engine.pipe import PipeContext, StepTrace, run_pipe


@dataclass(frozen=True)
class PageResult:
    loop: list[StepTrace] | None  # None: no page-level Loop
    extraction: ExtractionResult
    follow: FollowResult


def evaluate_page(
    page_type: PageType, scope: Any, ctx: PipeContext, parent_ref: int | None = None
) -> PageResult:
    loop, nodes = None, [scope]
    if page_type.items is not None:
        nodes, loop = run_pipe(page_type.items.each, scope, ctx)
    return PageResult(
        loop,
        extract(page_type, nodes, ctx),
        follow(page_type, scope, nodes, ctx, parent_ref),
    )
