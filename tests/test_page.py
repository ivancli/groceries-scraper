import yaml
from parsel import Selector

from groceries_scraper.config.models import PageType
from groceries_scraper.engine.page import evaluate_page
from groceries_scraper.engine.pipe import PipeContext, StepTrace

LISTING = """
<div class="tile"><a href="/p/a1">Milk</a></div>
<div class="tile"><a href="/p/b2">Cheese</a></div>
"""


def _page(source: str) -> PageType:
    return PageType.model_validate(yaml.safe_load(source))


def test_navigation_only_page_traces_its_loop() -> None:
    page = _page("""
    items: {each: {css: li.tile}}
    follow: [{select: {css: "a::attr(href)"}, scope: each, page_type: product}]
    """)

    result = evaluate_page(page, Selector(text=LISTING), PipeContext(url="https://x.example/"))

    assert result.loop == [StepTrace("css", [])]
    assert result.follow.requests == []


def test_loop_nodes_are_shared_by_records_and_follow_rules() -> None:
    page = _page("""
    record: product
    items: {each: {css: div.tile}}
    fields: {name: {css: "a::text"}}
    follow: [{select: {css: "a::attr(href)"}, scope: each, page_type: product}]
    """)

    result = evaluate_page(page, Selector(text=LISTING), PipeContext(url="https://x.example/"))

    assert result.loop is not None and len(result.loop[0].output) == 2
    assert [r.data["name"] for r in result.extraction.records] == ["Milk", "Cheese"]
    assert [r.url for r in result.follow.requests] == [
        "https://x.example/p/a1",
        "https://x.example/p/b2",
    ]


def test_page_without_loop_has_no_loop_trace() -> None:
    result = evaluate_page(_page("record: product"), Selector(text=LISTING), PipeContext())

    assert result.loop is None
    assert len(result.extraction.records) == 1
