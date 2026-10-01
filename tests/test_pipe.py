from pathlib import Path
from typing import Any

import pytest
import yaml
from parsel import Selector
from pydantic import TypeAdapter

from groceries_scraper.config.models import TEMPLATE_NAMES, Pipe
from groceries_scraper.engine.pipe import PipeContext, StepTrace, run_pipe, template_scope

LISTING = Selector(
    text="""
    <nav class="crumbs"><a>Dairy</a></nav>
    <div class="tile" data-sku="A1">
      <a href="/p/a1">Milk</a><span class="price">$1,299.50</span>
    </div>
    <div class="tile" data-sku="B2">
      <a href="/p/b2">Cheese</a><span class="price">$4.00</span>
    </div>
    """
)

NEXT_DATA = Selector(text=(Path(__file__).parent / "fixtures" / "next_data.html").read_text())


def _pipe(source: str) -> Pipe:
    return TypeAdapter(Pipe).validate_python(yaml.safe_load(source))


def _run(source: str, scope: Any = LISTING, **ctx: Any) -> list[Any]:
    values, _ = run_pipe(_pipe(source), scope, PipeContext(**ctx))
    return values


def test_css_returns_all_matches() -> None:
    assert _run("{css: 'a::attr(href)'}") == ["/p/a1", "/p/b2"]


def test_relative_xpath_in_a_node_scope_only_sees_that_node() -> None:
    second_tile = LISTING.css("div.tile")[1]

    assert _run("{xpath: './/a/text()'}", second_tile) == ["Cheese"]
    assert _run("{xpath: '@data-sku'}", second_tile) == ["B2"]


def test_absolute_xpath_in_a_node_scope_sees_the_whole_document() -> None:
    second_tile = LISTING.css("div.tile")[1]

    assert _run("{xpath: '//nav//text()'}", second_tile) == ["Dairy"]


def test_next_data_chain_from_html_through_json() -> None:
    pipe = """
    - css: "script#__NEXT_DATA__::text"
    - parse: json
    - jsonpath: "$.props.pageProps.product.images[*].url"
    """

    assert _run(pipe, NEXT_DATA) == ["/img/a1-1.jpg", "/img/a1-2.jpg"]


def test_parse_html_switches_to_an_html_scope() -> None:
    pipe = "[{jsonpath: '$.body'}, {parse: html}, {css: 'b::text'}]"

    assert _run(pipe, {"body": "<p>Hi <b>there</b></p>"}) == ["there"]


def test_every_step_output_is_traced() -> None:
    _, trace = run_pipe(
        _pipe("[{css: 'span.price::text'}, {regex: '([\\d.]+)'}]"), LISTING, PipeContext()
    )

    assert trace == [
        StepTrace("css", ["$1,299.50", "$4.00"]),
        StepTrace("regex", ["1", "4.00"]),
    ]


@pytest.mark.parametrize(
    ("pipe", "scope", "error"),
    [
        ("{css: a}", {"a": 1}, "css needs an HTML Scope, got dict"),
        ("{jsonpath: '$.a'}", LISTING, "jsonpath needs a JSON Scope, got HTML (add `parse: json`)"),
        ("{xpath: '//['}", LISTING, "ValueError: XPath error: Invalid expression in //["),
        ("{parse: json}", "{not json", "JSONDecodeError"),
        (
            "{jsonpath: '$.a'}",
            '{"a": 1}',
            "jsonpath needs a JSON Scope, got text (add `parse: json`)",
        ),
    ],
)
def test_step_error_is_traced_and_pipe_yields_nothing(pipe: str, scope: Any, error: str) -> None:
    values, trace = run_pipe(_pipe(pipe), scope, PipeContext())

    assert values == []
    assert trace[-1].output == []
    assert trace[-1].error is not None and trace[-1].error.startswith(error)


@pytest.mark.parametrize(
    ("pipe", "expected"),
    [
        pytest.param(
            "[{css: '.price::text'}, {regex: '\\$([\\d,.]+)'}, {replace: [',', '']}]",
            ["1299.50", "4.00"],
            id="regex-group-then-replace",
        ),
        pytest.param("[{css: '.price::text'}, {regex: '\\d+'}]", ["1", "4"], id="regex-full-match"),
        pytest.param("[{css: '.price::text'}, {regex: 'free'}]", [], id="regex-no-match-drops"),
        pytest.param(
            "[{css: 'div.tile'}, {regex: 'Milk'}]", ["Milk"], id="regex-reads-text-content"
        ),
        pytest.param("[{css: 'nav'}, strip]", ["Dairy"], id="strip"),
        pytest.param("[{css: 'a::text'}, lower]", ["dairy", "milk", "cheese"], id="lower"),
        pytest.param("[{css: 'a::text'}, {upper: true}]", ["DAIRY", "MILK", "CHEESE"], id="upper"),
        pytest.param(
            "[{css: '.price::text'}, {split: '.'}]",
            ["$1,299", "50", "$4", "00"],
            id="split-fans-out",
        ),
        pytest.param("[{css: 'div.tile a::text'}, {join: ' | '}]", ["Milk | Cheese"], id="join"),
    ],
)
def test_text_transforms(pipe: str, expected: list[Any]) -> None:
    assert _run(pipe) == expected


def test_urljoin_resolves_against_the_response_url() -> None:
    pipe = "[{css: 'div.tile a::attr(href)'}, urljoin]"

    assert _run(pipe, url="https://shop.example/c/dairy") == [
        "https://shop.example/p/a1",
        "https://shop.example/p/b2",
    ]


def test_urljoin_without_a_response_url_is_a_step_error() -> None:
    values, trace = run_pipe(_pipe("[{css: 'a::attr(href)'}, urljoin]"), LISTING, PipeContext())

    assert values == []
    assert trace[-1].error == "urljoin needs the response URL"


def test_text_transform_on_a_non_text_value_is_a_step_error() -> None:
    values, trace = run_pipe(_pipe("[{jsonpath: '$.a'}, lower]"), {"a": {"b": 1}}, PipeContext())

    assert values == []
    assert trace[-1].error == "lower needs text, got dict"


def _error(pipe: str, scope: Any = LISTING, **ctx: Any) -> str | None:
    values, trace = run_pipe(_pipe(pipe), scope, PipeContext(**ctx))
    assert values == []
    return trace[-1].error


def test_var_reads_a_passed_variable_or_a_session_variable() -> None:
    ctx = {"variables": {"sku": "A1"}, "session": {"csrf": "t0k"}}

    assert _run("{var: sku}", **ctx) == ["A1"]
    assert _run("{var: session.csrf}", **ctx) == ["t0k"]


def test_unknown_var_is_a_step_error() -> None:
    assert _error("{var: sku}") == "unknown Variable `sku`"
    assert _error("{var: session.csrf}") == "unknown Variable `session.csrf`"


def test_template_sees_value_variables_session_and_env() -> None:
    pipe = """
    - css: "div.tile::attr(data-sku)"
    - template: "{{ value }}/{{ cat }}/{{ session.csrf }}/{{ env.KEY }}"
    """
    ctx = {"variables": {"cat": "dairy"}, "session": {"csrf": "t0k"}, "env": {"KEY": "k"}}

    assert _run(pipe, **ctx) == ["A1/dairy/t0k/k", "B2/dairy/t0k/k"]


def test_template_undefined_name_is_a_step_error() -> None:
    assert _error("{template: '{{ nope }}'}") == "UndefinedError: 'nope' is undefined"


def test_template_is_sandboxed() -> None:
    error = _error("{template: '{{ value.__class__.__mro__ }}'}", scope="x")

    assert error is not None and error.startswith("SecurityError")


def shout(value: Any, ctx: PipeContext) -> Any:
    return f"{value}!{ctx.variables['suffix']}"


def explode(value: Any, ctx: PipeContext) -> Any:
    raise RuntimeError("boom")


def test_fn_calls_the_named_callable_with_value_and_context() -> None:
    pipe = f"[{{css: 'div.tile a::text'}}, {{fn: '{__name__}:shout'}}]"

    assert _run(pipe, variables={"suffix": "?"}) == ["Milk!?", "Cheese!?"]


def test_fn_failures_are_step_errors() -> None:
    assert _error(f"{{fn: '{__name__}:explode'}}") == "RuntimeError: boom"
    assert _error(f"{{fn: '{__name__}:missing'}}") == (
        f"AttributeError: module '{__name__}' has no attribute 'missing'"
    )
    assert (
        _error("{fn: 'no_such_module:f'}")
        == "ModuleNotFoundError: No module named 'no_such_module'"
    )


def test_non_node_xpath_results_are_plain_values() -> None:
    assert _run("{xpath: 'count(//div)'}") == [2.0]


def test_steps_after_a_no_match_yield_nothing() -> None:
    assert _run("[{css: 'table'}, {join: ','}]") == []
    assert _run("[{css: 'table::attr(href)'}, urljoin]") == []


def test_config_reserves_exactly_the_names_templates_inject() -> None:
    assert set(template_scope("v", PipeContext())) == set(TEMPLATE_NAMES)


def test_template_names_cannot_be_shadowed_by_variables() -> None:
    ctx = PipeContext(variables={"value": "var", "session": "var"}, session={"csrf": "tok"})

    scope = template_scope("selected", ctx)

    assert (scope["value"], scope["session"]) == ("selected", {"csrf": "tok"})
