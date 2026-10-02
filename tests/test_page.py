import yaml
from parsel import Selector

from groceries_scraper.config.models import PageType
from groceries_scraper.engine.page import evaluate_page, evaluate_response
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
    assert [r.request.url for r in result.follow.requests] == [
        "https://x.example/p/a1",
        "https://x.example/p/b2",
    ]


def test_page_without_loop_has_no_loop_trace() -> None:
    result = evaluate_page(_page("record: product"), Selector(text=LISTING), PipeContext())

    assert result.loop is None
    assert len(result.extraction.records) == 1


def test_response_body_is_parsed_as_the_page_types_response_kind() -> None:
    page = _page("record: product\nresponse: json\nfields: {name: {jsonpath: $.name}}")

    result = evaluate_response(
        page, b'{"name": "Milk"}', "text/html", PipeContext(url="https://x.example/")
    )

    assert result.extraction.records[0].data == {"name": "Milk"}


def test_html_body_is_decoded_with_the_content_type_charset() -> None:
    page = _page("record: product\nfields: {name: {css: 'h1::text'}}")
    body = "<h1>Crème</h1>".encode("latin-1")

    result = evaluate_response(page, body, "text/html; charset=ISO-8859-1", PipeContext())

    assert result.extraction.records[0].data == {"name": "Crème"}


def test_html_body_without_a_charset_is_utf8() -> None:
    page = _page("record: product\nfields: {name: {css: 'h1::text'}}")

    result = evaluate_response(page, "<h1>Crème</h1>".encode(), None, PipeContext())

    assert result.extraction.records[0].data == {"name": "Crème"}
