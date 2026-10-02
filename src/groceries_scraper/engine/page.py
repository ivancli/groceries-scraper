"""One response through a Page Type: the Loop runs once, shared by extraction and Follow Rules."""

import codecs
import json
import re
from dataclasses import dataclass
from typing import Any

from parsel import Selector

from groceries_scraper.config.models import PageType, ScopeKind
from groceries_scraper.engine.extract import ExtractionResult, extract
from groceries_scraper.engine.follow import FollowResult, follow
from groceries_scraper.engine.pipe import PipeContext, StepTrace, run_pipe


@dataclass(frozen=True)
class PageResult:
    loop: list[StepTrace] | None  # None: no page-level Loop
    extraction: ExtractionResult
    follow: FollowResult


def evaluate_response(
    page_type: PageType,
    body: bytes,
    content_type: str | None,
    ctx: PipeContext,
    parent_ref: int | None = None,
) -> PageResult:
    """The Page Type's `response:` decides the Scope; the content type only supplies a charset."""
    scope = parse_scope(page_type.response, body, content_type)
    return evaluate_page(page_type, scope, ctx, parent_ref)


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


_CHARSET = re.compile(r"charset=[\"']?([\w.:-]+)", re.I)


def parse_scope(response: ScopeKind, body: bytes, content_type: str | None) -> Any:
    if response == "json":
        return json.loads(body)  # detects UTF-8/16/32 itself
    return Selector(text=body.decode(_charset(content_type), errors="replace"))


def _charset(content_type: str | None) -> str:
    match = _CHARSET.search(content_type or "")
    if match:
        try:
            return codecs.lookup(match.group(1)).name
        except LookupError:
            pass
    return "utf-8"
