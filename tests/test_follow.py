import json
from dataclasses import replace
from typing import Any

import yaml
from parsel import Selector

from groceries_scraper.config.models import PageType
from groceries_scraper.engine.follow import FollowRequest, FollowResult
from groceries_scraper.engine.page import evaluate_page
from groceries_scraper.engine.pipe import PipeContext
from groceries_scraper.engine.request import RenderedRequest

URL = "https://shop.example/c/dairy?page=1"

LISTING = """
<nav class="crumbs"><a>Dairy</a></nav>
<div class="tile" data-sku="A1"><a class="tile-link" href="/p/a1">Milk</a>
  <span class="price">$1,234.50</span></div>
<div class="tile" data-sku="B2"><a class="tile-link" href="/p/b2">Cheese</a>
  <span class="price">$4.00</span></div>
<a class="next" href="?page=2">Next</a>
"""


def _follow(source: str, scope: Any, ctx: PipeContext | None = None) -> FollowResult:
    page_type = PageType.model_validate(yaml.safe_load(source))
    return evaluate_page(page_type, scope, ctx or PipeContext(url=URL), parent_ref=7).follow


def test_pagination_rule_targets_its_own_page_type() -> None:
    page = """
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
    """

    result = _follow(page, Selector(text=LISTING))

    assert result.requests == [
        FollowRequest(
            RenderedRequest("GET", "https://shop.example/c/dairy?page=2"),
            page_type="listing",
            variables={},
            parent_ref=7,
        )
    ]


def test_page_scope_follows_every_selected_value_with_inherited_variables() -> None:
    page = """
    follow:
      - select: {css: "a.tile-link::attr(href)"}
        page_type: product
    """
    ctx = PipeContext(url=URL, variables={"category": "Dairy"})

    result = _follow(page, Selector(text=LISTING), ctx)

    assert [(r.request.url, r.variables) for r in result.requests] == [
        ("https://shop.example/p/a1", {"category": "Dairy"}),
        ("https://shop.example/p/b2", {"category": "Dairy"}),
    ]


def test_each_scope_passes_per_tile_variables_to_the_matching_child_request() -> None:
    page = """
    items:
      each: {css: "div.tile"}
    follow:
      - select: {css: "a.tile-link::attr(href)"}
        scope: each
        page_type: product
        pass:
          sku: {css: "::attr(data-sku)"}
          price: [{css: ".price::text"}, {regex: '([\\d,.]+)'}, {replace: [",", ""]}]
          category: {xpath: "//nav[@class='crumbs']//text()", absolute: true}
          source: {var: origin}
    """
    ctx = PipeContext(url=URL, variables={"origin": "dairy", "sku": "inherited"})

    result = _follow(page, Selector(text=LISTING), ctx)

    assert [(r.request.url, r.variables) for r in result.requests] == [
        (
            "https://shop.example/p/a1",
            {
                "origin": "dairy",
                "sku": "A1",
                "price": "1234.50",
                "category": "Dairy",
                "source": "dairy",
            },
        ),
        (
            "https://shop.example/p/b2",
            {
                "origin": "dairy",
                "sku": "B2",
                "price": "4.00",
                "category": "Dairy",
                "source": "dairy",
            },
        ),
    ]


def test_with_session_rerenders_with_new_session_variables_and_keeps_the_rest() -> None:
    page = """
    follow:
      - select: {css: "a.tile-link::attr(href)"}
        as: href
        page_type: product
        pass:
          sku: {css: "div.tile::attr(data-sku)"}
        request:
          method: POST
          url: "/api/product?sku={{ sku }}"
          headers: {X-CSRF-Token: "{{ session.csrf }}"}
          json: {path: "{{ href }}"}
    """
    ctx = PipeContext(url=URL, session={"csrf": "old"})
    original = _follow(page, Selector(text=LISTING), ctx).requests[0]

    retried = original.with_session({"csrf": "new"})

    headers = {**original.request.headers, "X-CSRF-Token": "new"}
    assert retried == replace(original, request=replace(original.request, headers=headers))


def test_with_session_leaves_a_request_without_a_template_unchanged() -> None:
    start = FollowRequest(RenderedRequest("GET", URL), "listing", {}, None)

    assert start.with_session({"csrf": "new"}) == start


def test_post_json_template_renders_session_variables_and_passed_variables() -> None:
    page = """
    items:
      each: {css: "div.tile"}
    follow:
      - select: {css: "a.tile-link::attr(href)"}
        scope: each
        as: href
        page_type: product_api
        pass:
          sku: {css: "::attr(data-sku)"}
        request:
          method: POST
          url: "https://shop.example/api/product"
          headers: {X-CSRF-Token: "{{ session.csrf }}", X-Key: "{{ env.SHOP_KEY }}"}
          json: {sku: "{{ sku }}", path: "{{ href }}", same: "{{ value }}", tags: ["{{ sku }}"]}
    """
    ctx = PipeContext(url=URL, session={"csrf": "tok"}, env={"SHOP_KEY": "k"})

    first, second = _follow(page, Selector(text=LISTING), ctx).requests

    assert first.request.method == "POST"
    assert first.request.url == "https://shop.example/api/product"
    assert first.request.headers == {
        "X-CSRF-Token": "tok",
        "X-Key": "k",
        "Content-Type": "application/json",
    }
    assert first.request.body is not None
    assert json.loads(first.request.body) == {
        "sku": "A1",
        "path": "/p/a1",
        "same": "/p/a1",
        "tags": ["A1"],
    }
    assert first.variables == {"sku": "A1"}
    assert json.loads(second.request.body or "")["sku"] == "B2"


def test_form_and_raw_body_templates() -> None:
    page = """
    response: json
    follow:
      - select: {jsonpath: "$.next"}
        page_type: listing
        request: {method: POST, form: {page: "{{ value }}"}}
      - select: {jsonpath: "$.next"}
        page_type: listing
        request: {method: POST, url: "/search", body: "page={{ value }}"}
    """

    form, raw = _follow(page, {"next": "2"}).requests

    assert (form.request.url, form.request.body) == ("https://shop.example/c/2", "page=2")
    assert form.request.headers == {"Content-Type": "application/x-www-form-urlencoded"}
    assert (raw.request.url, raw.request.body, raw.request.headers) == (
        "https://shop.example/search",
        "page=2",
        {},
    )


def test_rendering_error_is_traced_and_other_rules_still_follow() -> None:
    page = """
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
        request: {url: "{{ missing }}"}
      - select: {css: "a.next::attr(href)"}
        page_type: listing
    """

    result = _follow(page, Selector(text=LISTING))

    assert [r.request.url for r in result.requests] == ["https://shop.example/c/dairy?page=2"]
    [error] = [t for t in result.trace if t.error]
    assert (error.rule, error.path) == (0, "request")
    assert error.error is not None and "missing" in error.error


def test_select_without_match_issues_no_request_and_traces_steps() -> None:
    result = _follow(
        "follow: [{select: {css: 'a.prev::attr(href)'}, page_type: listing}]",
        Selector(text=LISTING),
    )

    assert result.requests == []
    [select] = [t for t in result.trace if t.path == "select"]
    assert select.rule == 0 and select.steps[0].output == []


def test_pass_without_match_passes_null() -> None:
    page = """
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
        pass: {sku: {css: "::attr(data-missing)"}}
    """

    result = _follow(page, Selector(text=LISTING))

    assert result.requests[0].variables == {"sku": None}
    [entry] = [t for t in result.trace if t.path == "pass.sku"]
    assert entry.error is None


def test_json_value_that_is_one_expression_keeps_its_native_type() -> None:
    page = """
    response: json
    follow:
      - select: {jsonpath: "$.next"}
        page_type: listing
        pass: {page: {jsonpath: "$.page"}, sale: {jsonpath: "$.sale"}, text: {jsonpath: "$.text"}}
        request:
          method: POST
          json:
            page: "{{ page }}"
            sale: "{{ sale }}"
            text: "{{ text }}"
            text_int: "{{ text | int }}"
            ids: "{{ [page, text] }}"
            mixed: "p{{ page }}"
            two: "{{ page }}{{ text }}"
            spaced: " {{ page }}"
            literal: 2
    """

    [request] = _follow(page, {"next": "/n", "page": 2, "sale": True, "text": "3"}).requests

    assert json.loads(request.request.body or "") == {
        "page": 2,
        "sale": True,
        "text": "3",
        "text_int": 3,
        "ids": [2, "3"],
        "mixed": "p2",
        "two": "23",
        "spaced": " 2",
        "literal": 2,
    }


def test_json_expression_with_undefined_variable_is_a_render_error() -> None:
    page = """
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
        request: {method: POST, json: {page: "{{ missing }}"}}
    """

    result = _follow(page, Selector(text=LISTING))

    assert result.requests == []
    [error] = [t for t in result.trace if t.error]
    assert error.error is not None and "missing" in error.error


def test_json_expression_is_sandboxed() -> None:
    page = """
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
        request: {method: POST, json: {x: "{{ value.__class__ }}"}}
    """

    result = _follow(page, Selector(text=LISTING))

    assert result.requests == []
    [error] = [t for t in result.trace if t.error]
    assert error.error is not None and error.error.startswith("SecurityError")
