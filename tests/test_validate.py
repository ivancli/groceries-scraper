from pathlib import Path
from typing import Any

import yaml

from groceries_scraper.config import Findings, check_site, parse_site

DEFAULTS = yaml.safe_load((Path(__file__).parents[1] / "defaults.yaml").read_text())


def _check(page_types: dict[str, Any], **sections: Any) -> Findings:
    data = {
        "site": "s",
        "start": [{"url": "https://x.example/", "page_type": "listing"}],
        "page_types": page_types,
        **sections,
    }
    return check_site(parse_site(data, DEFAULTS))


def _errors(page_types: dict[str, Any], **sections: Any) -> list[str]:
    return _check(page_types, **sections).errors


# --- Page Type references ---------------------------------------------------


def test_start_request_to_unknown_page_type_is_an_error() -> None:
    errors = _errors({"product": {}}, start=[{"url": "https://x/", "page_type": "listng"}])

    assert errors == ["start[0].page_type: unknown Page Type `listng`"]


def test_follow_rule_to_unknown_page_type_is_an_error() -> None:
    errors = _errors(
        {"listing": {"follow": [{"select": {"css": "a::attr(href)"}, "page_type": "pdp"}]}}
    )

    assert errors == ["page_types.listing.follow[0].page_type: unknown Page Type `pdp`"]


def test_unreachable_page_type_is_a_warning_not_an_error() -> None:
    findings = _check(
        {
            "listing": {"follow": [{"select": {"css": "a::attr(href)"}, "page_type": "product"}]},
            "product": {},
            "orphan": {"follow": [{"select": {"css": "a::attr(href)"}, "page_type": "orphan"}]},
        }
    )

    assert findings.errors == []
    assert findings.warnings == ["page_types.orphan: unreachable from any Start Request"]


# --- Scope types ------------------------------------------------------------


def _record_page(response: str, fields: dict[str, Any], **page: Any) -> dict[str, Any]:
    return {"listing": {"response": response, "record": "product", "fields": fields, **page}}


def test_css_on_a_json_response_is_an_error() -> None:
    errors = _errors(_record_page("json", {"name": {"css": "h1::text"}}))

    assert errors == [
        "page_types.listing.fields.name: css needs an HTML Scope, got JSON (add `parse: html`)"
    ]


def test_jsonpath_on_html_needs_parse_json_first() -> None:
    errors = _errors(
        _record_page(
            "html",
            {
                "bad": {"jsonpath": "$.name"},
                "good": [{"css": "script#data::text"}, {"parse": "json"}, {"jsonpath": "$.name"}],
            },
        )
    )

    assert errors == [
        "page_types.listing.fields.bad: jsonpath needs a JSON Scope, got HTML (add `parse: json`)"
    ]


def test_selector_after_a_text_step_is_an_error() -> None:
    errors = _errors(_record_page("html", {"name": [{"css": "h1"}, "strip", {"css": "b"}]}))

    assert errors == ["page_types.listing.fields.name[2]: css needs an HTML Scope, got text"]


def test_nested_fields_take_the_scope_of_their_loop() -> None:
    errors = _errors(
        _record_page(
            "json",
            {
                "variants": {
                    "type": "array",
                    "each": {"jsonpath": "$.variants[*]"},
                    "fields": {"size": {"xpath": "./size"}},
                }
            },
        )
    )

    assert errors == [
        "page_types.listing.fields.variants.fields.size: "
        "xpath needs an HTML Scope, got JSON (add `parse: html`)"
    ]


def test_each_scoped_follow_rule_takes_the_page_loop_scope() -> None:
    errors = _errors(
        {
            "listing": {
                "response": "html",
                "items": {"each": [{"css": "script::text"}, {"parse": "json"}]},
                "follow": [
                    {
                        "select": {"jsonpath": "$.url"},
                        "scope": "each",
                        "page_type": "listing",
                        "pass": {"sku": {"css": "::attr(data-sku)"}},
                    }
                ],
            }
        }
    )

    assert errors == [
        "page_types.listing.follow[0].pass.sku: "
        "css needs an HTML Scope, got JSON (add `parse: html`)"
    ]


# --- Absolute XPath -----------------------------------------------------------


def test_absolute_xpath_inside_a_loop_needs_absolute_true() -> None:
    errors = _errors(
        _record_page(
            "html",
            {
                "bad": {"xpath": "//h1/text()"},
                "bad_grouped": {"xpath": "(/html//h1)[1]"},
                "relative": {"xpath": ".//h1/text()"},
                "marked": {"xpath": "//h1/text()", "absolute": True},
            },
            items={"each": {"xpath": "//div[@class='tile']"}},  # page level: not in a Loop
            follow=[
                {"select": {"xpath": "//a/@href"}, "scope": "each", "page_type": "listing"},
                {
                    "select": [{"css": "script::text"}, {"parse": "html"}, {"xpath": "//a/@href"}],
                    "scope": "each",
                    "page_type": "listing",
                },
            ],
        )
    )

    assert errors == [
        "page_types.listing.fields.bad: "
        "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`",
        "page_types.listing.fields.bad_grouped: "
        "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`",
        "page_types.listing.follow[0].select: "
        "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`",
    ]


def test_absolute_xpath_outside_any_loop_is_fine() -> None:
    assert _errors(_record_page("html", {"name": {"xpath": "//h1/text()"}})) == []


def test_absolute_xpath_in_nested_loop_and_each_scoped_follow_rule() -> None:
    errors = _errors(
        {
            "listing": {
                "record": "product",
                "fields": {
                    "variants": {
                        "type": "array",
                        "each": {"css": "li"},
                        "fields": {"size": {"xpath": "//b"}},
                    }
                },
                "follow": [{"select": {"xpath": "//a/@href"}, "page_type": "listing"}],
            }
        }
    )

    assert errors == [
        "page_types.listing.fields.variants.fields.size: "
        "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`"
    ]


# --- Variable reachability ----------------------------------------------------


def _to(page_type: str, **rule: Any) -> dict[str, Any]:
    return {"select": {"css": "a::attr(href)"}, "page_type": page_type, **rule}


_PRODUCT = {"record": "product", "fields": {"sku": {"var": "sku"}}}


def test_variable_passed_by_the_parent_is_fine() -> None:
    errors = _errors(
        {
            "listing": {"follow": [_to("product", **{"pass": {"sku": {"css": "b::text"}}})]},
            "product": _PRODUCT,
        }
    )

    assert errors == []


def test_renaming_a_pass_key_without_updating_the_child_is_an_error() -> None:
    errors = _errors(
        {
            "listing": {"follow": [_to("product", **{"pass": {"code": {"css": "b::text"}}})]},
            "product": _PRODUCT,
        }
    )

    assert errors == [
        "page_types.product.fields.sku: Variable `sku` is not passed on every path to "
        "Page Type `product` (missing via page_types.listing.follow[0])"
    ]


def test_variable_must_be_passed_on_every_path() -> None:
    errors = _errors(
        {
            "listing": {"follow": [_to("product", **{"pass": {"sku": {"css": "b::text"}}})]},
            "product": _PRODUCT,
        },
        start=[
            {"url": "https://x/c", "page_type": "listing"},
            {"url": "https://x/p", "page_type": "product"},
        ],
    )

    assert errors == [
        "page_types.product.fields.sku: Variable `sku` is not passed on every path to "
        "Page Type `product` (missing via start[1])"
    ]


def test_variables_carry_down_the_chain_and_through_pagination() -> None:
    errors = _errors(
        {
            "listing": {
                "follow": [
                    _to("listing"),
                    _to("brand", **{"pass": {"sku": {"css": "b::text"}}}),
                ]
            },
            "brand": {"follow": [_to("product"), _to("brand")]},
            "product": _PRODUCT,
        }
    )

    assert errors == []


def test_session_variable_must_be_extracted_by_session_setup() -> None:
    session = {"setup": [{"request": {"url": "https://x/"}, "extract": {"csrf": {"css": "m"}}}]}
    fields = {"a": {"var": "session.csrf"}, "b": {"var": "session.token"}}

    errors = _errors(_record_page("html", fields), session=session)

    assert errors == [
        "page_types.listing.fields.b: Session Variable `session.token` is not set by Session Setup"
    ]


def test_template_variables_are_checked_like_var_steps() -> None:
    session = {"setup": [{"request": {"url": "https://x/"}, "extract": {"csrf": {"css": "m"}}}]}
    rule = _to(
        "listing",
        **{
            "as": "href",
            "pass": {"sku": {"css": "b::text"}},
            "request": {
                "url": "{{ href }}?sku={{ sku }}&v={{ value }}",
                "headers": {"X-CSRF": "{{ session.csrf }}", "X-Key": "{{ env.KEY }}"},
                "json": {"q": ["{{ query }}"]},
            },
        },
    )
    fields = {"label": [{"css": "h1::text"}, {"template": "{{ value }}-{{ colour }}"}]}

    errors = _errors(_record_page("html", fields, follow=[rule]), session=session)

    assert errors == [
        "page_types.listing.fields.label[1]: Variable `colour` is not passed on every path to "
        "Page Type `listing` (missing via start[0], page_types.listing.follow[0])",
        "page_types.listing.follow[0].request.json.q[0]: Variable `query` is not passed on "
        "every path to Page Type `listing` (missing via start[0], page_types.listing.follow[0])",
    ]


def test_session_setup_sees_only_earlier_steps_session_variables() -> None:
    session = {
        "setup": [
            {
                "request": {"url": "https://x/?t={{ session.token }}"},
                "extract": {"token": {"css": "m"}},
            },
            {
                "request": {"url": "https://x/", "headers": {"T": "{{ session.token }}"}},
                "extract": {"id": {"var": "sku"}},
            },
        ]
    }

    errors = _errors({"listing": {}}, session=session)

    assert errors == [
        "session.setup[0].request.url: "
        "Session Variable `session.token` is not set by Session Setup",
        "session.setup[1].extract.id: Variable `sku` is not available in Session Setup",
    ]


# --- Custom functions and templates -----------------------------------------------


def test_fn_must_be_importable() -> None:
    fields = {
        "ok": {"fn": "json:dumps"},
        "no_module": {"fn": "no_such_pkg.mod:parse"},
        "no_attr": {"fn": "json:no_such_fn"},
        "not_callable": {"fn": "json:__name__"},
    }

    errors = _errors(_record_page("html", fields))

    assert errors == [
        "page_types.listing.fields.no_module: cannot import `no_such_pkg.mod`: "
        "No module named 'no_such_pkg'",
        "page_types.listing.fields.no_attr: `json` has no attribute `no_such_fn`",
        "page_types.listing.fields.not_callable: `json:__name__` is not callable",
    ]


def test_templates_must_compile() -> None:
    rule = _to("listing", request={"url": "{{ value ", "body": "{% if %}"})
    fields = {"x": [{"css": "h1::text"}, {"template": "{{ value | }}"}]}

    errors = _errors(_record_page("html", fields, follow=[rule]))

    assert [e.split(": invalid template")[0] for e in errors] == [
        "page_types.listing.fields.x[1]",
        "page_types.listing.follow[0].request.url",
        "page_types.listing.follow[0].request.body",
    ]


# --- Record Types and Health Checks -------------------------------------------------


def _emitting(*fields: str) -> dict[str, Any]:
    return {
        "listing": {"follow": [_to("pdp"), _to("api")]},
        "pdp": {"record": "product", "fields": {f: {"css": "b::text"} for f in fields}},
        "api": {"response": "json", "record": "product", "fields": {"sku": {"jsonpath": "$.sku"}}},
    }


def test_record_type_must_be_emitted_by_some_page_type() -> None:
    errors = _errors(
        _emitting("sku"), records={"product": {"key": ["sku"]}, "promo": {"key": ["id"]}}
    )

    assert errors == ["records.promo: no Page Type emits Record Type `promo`"]


def test_record_key_field_must_exist_on_every_emitting_page_type() -> None:
    errors = _errors(_emitting("name"), records={"product": {"key": ["sku"]}})

    assert errors == ["records.product.key[0]: Page Type `pdp` has no Field `sku`"]


def test_health_checks_must_name_known_record_types_and_fields() -> None:
    health = {
        "min_records": {"product": 1, "promo": 1},
        "max_null_ratio": {"sku": 0.1, "prise": 0.1},
    }

    errors = _errors(_emitting("sku", "price"), health=health)

    assert errors == [
        "health.min_records.promo: no Page Type emits Record Type `promo`",
        "health.max_null_ratio.prise: no Record-emitting Page Type has Field `prise`",
    ]
