from typing import Any

import pytest
import yaml
from parsel import Selector

from groceries_scraper.config.models import PageType
from groceries_scraper.engine.extract import DroppedRecord, ExtractionResult, Record
from groceries_scraper.engine.page import evaluate_page
from groceries_scraper.engine.pipe import PipeContext, StepTrace


def _extract(source: str, scope: Any) -> ExtractionResult:
    page_type = PageType.model_validate(yaml.safe_load(source))
    return evaluate_page(page_type, scope, PipeContext()).extraction


def test_page_type_without_record_emits_nothing() -> None:
    result = _extract("fields: {name: {jsonpath: '$.name'}}", {"name": "Milk"})

    assert result.records == []
    assert result.dropped == []


def test_page_type_without_loop_emits_one_record_of_its_record_type() -> None:
    page = """
    record: product
    response: json
    fields:
      name: {jsonpath: "$.names[*]", type: string}
      brand: {jsonpath: "$.brand"}
    """

    result = _extract(page, {"names": ["Milk", "Full Cream Milk"]})

    assert result.records == [Record("product", {"name": "Milk", "brand": None})]


def test_clean_numeric_text_coerces_to_a_number() -> None:
    result = _extract(
        "{record: product, fields: {price: {jsonpath: '$.p', type: number}}}", {"p": "3.50"}
    )

    assert result.records[0].data == {"price": 3.5}


def test_uncoercible_value_becomes_null_with_a_trace_error() -> None:
    result = _extract(
        "{record: product, fields: {price: {jsonpath: '$.p', type: number}}}", {"p": "$3.50"}
    )

    assert result.records[0].data == {"price": None}
    [entry] = [t for t in result.trace if t.path == "price"]
    assert entry.error == "cannot coerce '$3.50' to number"
    assert entry.steps == [StepTrace("jsonpath", ["$3.50"])]


@pytest.mark.parametrize(
    ("type_", "raw", "expected"),
    [
        ("string", "Milk", "Milk"),
        ("string", 1234, "1234"),
        ("string", True, None),
        ("number", 3, 3.0),
        ("number", "-1.5e2", -150.0),
        ("number", " 3.5", None),
        ("number", "inf", None),
        ("number", True, None),
        ("integer", "42", 42),
        ("integer", 7, 7),
        ("integer", 7.0, 7),
        ("integer", 7.5, None),
        ("integer", "7.0", None),
        ("boolean", True, True),
        ("boolean", "false", False),
        ("boolean", "yes", None),
        ("boolean", 1, None),
        (None, {"a": 1}, {"a": 1}),
    ],
)
def test_coercion_is_strict(type_: str | None, raw: Any, expected: Any) -> None:
    spec = {"jsonpath": "$.v"} | ({"type": type_} if type_ else {})
    page = yaml.safe_dump({"record": "product", "fields": {"v": spec}})

    assert _extract(page, {"v": raw}).records[0].data == {"v": expected}


def test_no_match_falls_back_to_default() -> None:
    page = """
    record: product
    fields: {on_sale: {jsonpath: "$.promo", type: boolean, default: false}}
    """

    assert _extract(page, {}).records[0].data == {"on_sale": False}


def test_coercion_failure_is_null_even_with_a_default() -> None:
    page = "{record: product, fields: {price: {jsonpath: '$.p', type: number, default: 0}}}"

    assert _extract(page, {"p": "$3.50"}).records[0].data == {"price": None}


def test_record_missing_a_required_field_is_dropped_with_reason_in_trace() -> None:
    page = """
    record: product
    fields:
      sku: {jsonpath: "$.sku", type: string, required: true}
      name: {jsonpath: "$.name"}
    """

    result = _extract(page, {"sku": None, "name": "Milk"})

    reason = "required Field `sku` is missing"
    assert result.records == []
    assert result.dropped == [DroppedRecord(0, reason, (reason,))]
    assert reason in [t.error for t in result.trace if t.path == "sku"]
    assert "name" in [t.path for t in result.trace]


def test_required_field_failing_coercion_is_dropped_with_the_coercion_error() -> None:
    page = "{record: product, fields: {price: {jsonpath: '$.p', type: number, required: true}}}"

    result = _extract(page, {"p": "$3.50"})

    reason = "required Field `price` is invalid: cannot coerce '$3.50' to number"
    assert result.dropped == [DroppedRecord(0, reason, ("required Field `price` is invalid",))]
    assert reason in [t.error for t in result.trace if t.path == "price"]


LISTING = Selector(
    text="""
    <div class="tile" data-sku="A1"><a>Milk</a><span class="price">1.50</span></div>
    <div class="tile"><a>Mystery</a></div>
    <div class="tile" data-sku="B2"><a>Cheese</a><span class="price">4</span></div>
    """
)


def test_page_level_loop_emits_one_record_per_node_and_counts_drops() -> None:
    page = """
    record: product
    items: {each: {css: div.tile}}
    fields:
      sku: {xpath: "@data-sku", type: string, required: true}
      name: {css: "a::text"}
      price: {css: ".price::text", type: number}
    """

    result = _extract(page, LISTING)

    assert [r.data for r in result.records] == [
        {"sku": "A1", "name": "Milk", "price": 1.5},
        {"sku": "B2", "name": "Cheese", "price": 4.0},
    ]
    reason = "required Field `sku` is missing"
    assert result.dropped == [DroppedRecord(1, reason, (reason,))]
    assert [(t.path, t.error) for t in result.trace if t.record == 1 and t.error] == [
        ("sku", "required Field `sku` is missing")
    ]


def test_page_level_loop_with_no_nodes_emits_nothing() -> None:
    page = "{record: product, items: {each: {css: li}}, fields: {name: {css: 'a::text'}}}"

    result = _extract(page, LISTING)

    assert result.records == []
    assert result.trace == []


def test_object_field_evaluates_nested_fields_in_its_pipe_scope() -> None:
    page = """
    record: product
    fields:
      nutrition:
        type: object
        jsonpath: "$.nutrition"
        fields:
          kcal: {jsonpath: "$.energy", type: integer}
      size:
        type: object
        fields:
          grams: {jsonpath: "$.g", type: integer}
    """

    result = _extract(page, {"nutrition": {"energy": "250"}, "g": 500})

    assert result.records[0].data == {"nutrition": {"kcal": 250}, "size": {"grams": 500}}
    assert "nutrition.kcal" in [t.path for t in result.trace]


def test_object_field_with_no_match_is_null() -> None:
    page = """
    record: product
    fields:
      nutrition: {type: object, jsonpath: "$.nutrition", fields: {kcal: {jsonpath: "$.e"}}}
    """

    assert _extract(page, {}).records[0].data == {"nutrition": None}


def test_array_items_take_all_matches_and_coerce_each() -> None:
    page = """
    record: product
    fields:
      sizes: {type: array, jsonpath: "$.sizes[*]", items: {type: integer}}
      tags: {type: array, jsonpath: "$.tags[*]", items: {type: string}}
    """

    result = _extract(page, {"sizes": ["1", "two", 3]})

    assert result.records[0].data == {"sizes": [1, None, 3], "tags": None}
    assert [(t.path, t.error) for t in result.trace if t.error] == [
        ("sizes[1]", "cannot coerce 'two' to integer")
    ]


def test_nested_loop_scopes_each_variant_to_its_own_product() -> None:
    page = """
    record: product
    items: {each: {css: section.product}}
    fields:
      name: {css: "h2::text"}
      variants:
        type: array
        each: {css: li.variant}
        fields:
          size: {css: ".size::text"}
          price: {css: ".price::text", type: number}
    """
    html = Selector(
        text="""
        <section class="product"><h2>Milk</h2><ul>
          <li class="variant"><b class="size">1L</b><i class="price">1.5</i></li>
          <li class="variant"><b class="size">2L</b><i class="price">2.8</i></li>
        </ul></section>
        <section class="product"><h2>Cheese</h2><ul>
          <li class="variant"><b class="size">200g</b><i class="price">$4</i></li>
        </ul></section>
        <section class="product"><h2>Bread</h2></section>
        """
    )

    result = _extract(page, html)

    assert [r.data for r in result.records] == [
        {
            "name": "Milk",
            "variants": [{"size": "1L", "price": 1.5}, {"size": "2L", "price": 2.8}],
        },
        {"name": "Cheese", "variants": [{"size": "200g", "price": None}]},
        {"name": "Bread", "variants": None},
    ]
    assert [(t.record, t.path, t.error) for t in result.trace if t.error] == [
        (1, "variants[0].price", "cannot coerce '$4' to number")
    ]


def test_required_field_inside_a_loop_element_drops_the_whole_record() -> None:
    page = """
    record: product
    fields:
      variants:
        type: array
        each: {jsonpath: "$.variants[*]"}
        fields:
          sku: {jsonpath: "$.sku", required: true}
    """

    result = _extract(page, {"variants": [{"sku": "A"}, {}]})

    assert result.dropped == [
        DroppedRecord(
            0,
            "required Field `variants[1].sku` is missing",
            ("required Field `variants[].sku` is missing",),
        )
    ]


def test_html_node_is_coerced_by_its_text() -> None:
    page = "{record: product, fields: {price: {css: 'span', type: number}}}"

    assert _extract(page, Selector(text="<span>3.5</span>")).records[0].data == {"price": 3.5}
