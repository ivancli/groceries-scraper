from pathlib import Path
from typing import Any

import pytest
import yaml

from groceries_scraper.config import (
    ConfigError,
    Findings,
    Site,
    check_site,
    check_sites,
    parse_site,
)

DEFAULTS = yaml.safe_load((Path(__file__).parents[1] / "defaults.yaml").read_text())


def _check(page_types: dict[str, Any], **sections: Any) -> Findings:
    # Declare every emitted Record Type unless a test is about `records:` itself.
    emitted = {page["record"] for page in page_types.values() if page.get("record")}
    data = {
        "site": "s",
        "start": [{"url": "https://x.example/", "page_type": "listing"}],
        "page_types": page_types,
        "records": dict.fromkeys(emitted, {}),
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


def test_selector_after_a_text_selecting_selector_is_an_error() -> None:
    fields = {
        "attr": [{"css": "a::attr(href)"}, {"css": "b"}],
        "text": [{"css": "h1::text"}, {"xpath": "./b"}],
        "xattr": [{"xpath": ".//a/@href"}, {"css": "b"}],
        "xtext": [{"xpath": ".//h1/text()"}, {"css": "b"}],
        "count": [{"xpath": "count(.//li)"}, {"css": "b"}],
        "nodes": [{"css": "div.tile"}, {"xpath": ".//a[@href]"}, {"css": "b"}],
        "union": [{"xpath": ".//a/@href | .//b"}, {"css": "b"}],  # unsure: never flagged
    }

    errors = _errors(_record_page("html", fields))

    assert errors == [
        f"page_types.listing.fields.{name}[1]: {kind} needs an HTML Scope, got text"
        for name, kind in [
            ("attr", "css"),
            ("text", "xpath"),
            ("xattr", "css"),
            ("xtext", "css"),
            ("count", "css"),
        ]
    ]


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
                "bad_union": {"xpath": ".//h1/text() | //h2[contains(., '|')]/text()"},
                "relative_union": {"xpath": ".//h1/text() | .//h2[@x='|//']/text()"},
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
        "page_types.listing.fields.bad_union: "
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
    fields = {
        "a": {"var": "session.csrf"},
        "b": {"var": "session.token"},
        "c": [{"css": "h1::text"}, {"template": "{{ session['csrf'] }}{{ session['tok'] }}"}],
    }

    errors = _errors(_record_page("html", fields), session=session)

    assert errors == [
        "page_types.listing.fields.b: Session Variable `session.token` is not set by Session Setup",
        "page_types.listing.fields.c[1]: "
        "Session Variable `session.tok` is not set by Session Setup",
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
        "page_types.listing.fields.no_module: cannot load `no_such_pkg.mod:parse`: "
        "ModuleNotFoundError: No module named 'no_such_pkg'",
        "page_types.listing.fields.no_attr: cannot load `json:no_such_fn`: "
        "AttributeError: module 'json' has no attribute 'no_such_fn'",
        "page_types.listing.fields.not_callable: cannot load `json:__name__`: "
        "TypeError: `json:__name__` is not callable",
    ]


def test_templates_must_compile() -> None:
    rule = _to("listing", request={"url": "{{ value ", "body": "{{ value | no_such_filter }}"})
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

    assert errors == ["records.promo: no reachable Page Type emits Record Type `promo`"]


def test_record_type_emitted_only_by_unreachable_page_types_is_an_error() -> None:
    page_types = {**_emitting("sku"), "orphan": {"record": "promo", "fields": {"id": {"css": "b"}}}}

    errors = _errors(page_types, records={"product": {}, "promo": {}})

    assert errors == ["records.promo: no reachable Page Type emits Record Type `promo`"]


def test_emitted_record_type_must_be_declared() -> None:
    errors = _errors(_emitting("sku"), records={"produt": {}})

    assert errors == [
        "records.produt: no reachable Page Type emits Record Type `produt`",
        "page_types.pdp.record: Record Type `product` is not declared in `records:`",
        "page_types.api.record: Record Type `product` is not declared in `records:`",
    ]


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
        "health.min_records.promo: no reachable Page Type emits Record Type `promo`",
        "health.max_null_ratio.prise: no Record-emitting Page Type has Field `prise`",
    ]


# --- Record Contracts ------------------------------------------------------


def _contracted(page_fields: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    page_types = {
        "listing": {"follow": [_to("pdp")]},
        "pdp": {"record": "product", "fields": page_fields},
    }
    return _errors(page_types, records={"product": {"fields": contract}})


def test_page_type_missing_a_contract_field_is_an_error() -> None:
    errors = _contracted(
        {"sku": {"css": "b::text", "type": "string"}},
        {"sku": {"type": "string"}, "name": {"type": "string"}},
    )

    assert errors == [
        "page_types.pdp.fields.name: missing; Record Type `product`'s contract declares it"
    ]


def test_page_type_field_outside_the_contract_is_an_error() -> None:
    errors = _contracted(
        {"sku": {"css": "b::text", "type": "string"}, "extra": {"css": "i::text"}},
        {"sku": {"type": "string"}},
    )

    assert errors == ["page_types.pdp.fields.extra: not in Record Type `product`'s contract"]


def test_page_type_field_type_must_match_the_contract() -> None:
    errors = _contracted(
        {"price": {"css": "b::text", "type": "string"}, "name": {"css": "h1::text"}},
        {"price": {"type": "number"}, "name": {"type": "string"}},
    )

    assert errors == [
        "page_types.pdp.fields.price: type `string`, but Record Type `product`'s contract "
        "says `number`",
        "page_types.pdp.fields.name: no type; add `type: string` to match Record Type "
        "`product`'s contract",
    ]


def test_contract_required_field_must_be_required_on_the_page_type() -> None:
    errors = _contracted(
        {
            "sku": {"css": "b::text", "type": "string"},
            "name": {"css": "h1::text", "type": "string", "required": True},
        },
        {"sku": {"type": "string", "required": True}, "name": {"type": "string"}},
    )

    assert errors == [
        "page_types.pdp.fields.sku: Record Type `product`'s contract requires `required: true`"
    ]


def test_nested_fields_are_checked_against_the_contract() -> None:
    page_fields = {
        "nutrition": {"type": "object", "fields": {"kcal": {"css": "i::text", "type": "number"}}},
        "images": {"type": "array", "css": "img::attr(src)", "items": {"type": "string"}},
        "variants": {
            "type": "array",
            "each": {"css": "li"},
            "fields": {"size": {"css": "::text"}},
        },
        "tags": {"type": "array", "css": "a::text", "items": {"type": "string"}},
    }
    contract = {
        "nutrition": {"type": "object", "fields": {"kcal": {"type": "integer"}}},
        "images": {"type": "array", "items": {"type": "integer"}},
        "variants": {"type": "array", "fields": {"size": {"type": "string"}}},
        "tags": {"type": "array", "fields": {"name": {"type": "string"}}},
    }

    errors = _contracted(page_fields, contract)

    assert errors == [
        "page_types.pdp.fields.nutrition.fields.kcal: type `number`, but Record Type `product`'s "
        "contract says `integer`",
        "page_types.pdp.fields.images.items: type `string`, but Record Type `product`'s "
        "contract says `integer`",
        "page_types.pdp.fields.variants.fields.size: no type; add `type: string` to match "
        "Record Type `product`'s contract",
        "page_types.pdp.fields.tags: array of scalars (`items`), but Record Type `product`'s "
        "contract says array of objects (`fields`)",
    ]


def test_every_reachable_emitter_is_checked_but_unreachable_ones_are_not() -> None:
    page_types = {
        **_emitting("sku"),
        "orphan": {"record": "product", "fields": {"name": {"css": "b::text"}}},
    }
    contract = {"sku": {"type": "string"}}

    errors = _errors(page_types, records={"product": {"fields": contract}})

    assert errors == [
        "page_types.api.fields.sku: no type; add `type: string` to match Record Type "
        "`product`'s contract",
        "page_types.pdp.fields.sku: no type; add `type: string` to match Record Type "
        "`product`'s contract",
    ]


# --- Locations ----------------------------------------------------------------


def test_location_variables_must_be_set_by_every_location() -> None:
    locations = {"melb": {"postcode": "3000", "store": "12"}, "syd": {"postcode": "2000"}}
    session = {
        "setup": [
            {"request": {"url": "https://x/", "json": {"pc": "{{ location.postcode }}"}}},
        ]
    }
    fields = {
        "a": {"var": "location.store"},
        "b": [{"css": "h1::text"}, {"template": "{{ location['region'] }}"}],
    }

    errors = _errors(_record_page("html", fields), session=session, locations=locations)

    assert errors == [
        "page_types.listing.fields.a: Location Variable `location.store` is not set by "
        "Location `syd`",
        "page_types.listing.fields.b[1]: Location Variable `location.region` is not set by "
        "Locations `melb`, `syd`",
    ]


def test_location_variables_need_declared_locations() -> None:
    fields = {"a": {"var": "location.postcode"}}

    assert _errors(_record_page("html", fields)) == [
        "page_types.listing.fields.a: Location Variable `location.postcode` is not set: "
        "the Site declares no Locations",
    ]


def test_a_session_pool_larger_than_the_start_requests_is_a_warning() -> None:
    findings = _check({"listing": {}}, session={"pool": 2, "setup": []})

    assert findings.errors == []
    assert findings.warnings == [
        "session.pool: 2 Sessions but 1 Start Request; Sessions without a Start Request stay idle"
    ]


# --- Accepts Rule -------------------------------------------------------------

PRODUCT_URL = "https://x.example/p/1"
PRICE_FIELDS: dict[str, Any] = {
    "url": {"css": "link::attr(href)", "type": "string", "required": True},
    "name": {"css": "h1::text", "type": "string", "required": True},
    "price": {"css": ".price::text", "type": "number", "required": True},
}
RETAILER: dict[str, Any] = {"key": "shop", "name": "Shop"}
ACCEPTS: dict[str, Any] = {
    "page_type": "product",
    "retailer": RETAILER,
    "url": "^https://x\\.example/p/",
    "examples": [PRODUCT_URL],
}


def _accepting(
    url: str = "^https://x\\.example/p/",
    *,
    examples: list[str] | None = None,
    fields: dict[str, Any] = PRICE_FIELDS,
    schedule: dict[str, Any] | None = None,
    retailer: Any = RETAILER,
    **page: Any,
) -> list[str]:
    page_types = {"listing": {}, "product": {"record": "product", "fields": fields, **page}}
    accepts = {**ACCEPTS, "url": url, "examples": examples or [PRODUCT_URL], "retailer": retailer}
    try:
        return _errors(page_types, accepts=accepts, schedule=schedule or {"every": "30m"})
    except ConfigError as exc:  # the pattern itself is checked by the schema
        return exc.errors


def test_an_accepts_rule_on_a_page_type_without_follow_rules_is_valid() -> None:
    assert _accepting() == []


def test_the_accepted_page_type_must_not_follow_links() -> None:
    follow = [{"select": {"css": "a::attr(href)"}, "page_type": "listing"}]

    assert _accepting(follow=follow) == [
        "accepts.page_type: Page Type `product` has Follow Rules; "
        "a Supplied Start Request must not start a crawl"
    ]


def test_the_accepts_pattern_must_be_anchored() -> None:
    assert _accepting("https://x\\.example/p/") == ["accepts.url: the pattern must start with `^`"]


@pytest.mark.parametrize(
    "construct",
    [
        "(?<=x)",
        "(?<!x)",
        "(?P<id>\\d+)",
        "(?<id>\\d+)",
        "(?i)",
        "(?P=id)",
        "\\A",
        "\\Z",
        "\\d{,3}",
        "\\d++",
        "\\d*+",
        "\\d?+",
        "\\d{2}+",
    ],
)
def test_the_accepts_pattern_stays_within_the_python_javascript_subset(construct: str) -> None:
    [error] = _accepting(f"^https://x\\.example/p/{construct}")

    assert error.startswith("accepts.url: ")
    assert "Python/JavaScript" in error


@pytest.mark.parametrize("escaped", ["\\(?<=", "[(?<=]", "\\\\d", "(?:a|b)?", "(?=1)", "(?!2)"])
def test_escapes_classes_and_plain_groups_are_within_the_subset(escaped: str) -> None:
    errors = _accepting(f"^https://x\\.example/p/{escaped}")

    assert not any("Python/JavaScript" in error for error in errors)


def test_the_accepts_pattern_must_compile() -> None:
    [error] = _accepting("^https://x\\.example/p/(")

    assert error.startswith("accepts.url: invalid pattern: ")


def test_every_accepts_example_must_match_the_pattern() -> None:
    errors = _accepting(examples=[PRODUCT_URL, "https://x.example/c/dairy"])

    assert errors == [
        "accepts.examples[1]: `https://x.example/c/dairy` does not match `accepts.url`"
    ]


def test_accepts_classes_match_ascii_only_as_in_javascript() -> None:
    errors = _accepting(
        "^https://x\\.example/p/\\d+$", examples=[PRODUCT_URL, "https://x.example/p/\u0661"]
    )

    assert errors == [
        "accepts.examples[1]: `https://x.example/p/\u0661` does not match `accepts.url`"
    ]


def test_an_accepts_rule_must_name_its_retailer() -> None:
    accepts = {key: value for key, value in ACCEPTS.items() if key != "retailer"}
    page_types = {"product": {"record": "product", "fields": PRICE_FIELDS}}

    with pytest.raises(ConfigError) as exc:
        _errors(page_types, accepts=accepts, schedule={"every": "30m"})

    assert exc.value.errors == ["accepts.retailer: Field required"]


@pytest.mark.parametrize("key", ["Chemist-Warehouse", "chemist-warehouse", "1shop", "_shop", ""])
def test_a_retailer_key_is_lowercase_letters_digits_and_underscores(key: str) -> None:
    assert _accepting(retailer={"key": key, "name": "Shop"}) == [
        f"accepts.retailer.key: `{key}` must be lowercase letters, digits and `_`, "
        "starting with a letter"
    ]


@pytest.mark.parametrize("name", ["", "  "])
def test_a_retailer_needs_a_name(name: str) -> None:
    assert _accepting(retailer={"key": "shop", "name": name}) == [
        "accepts.retailer.name: must not be blank"
    ]


# --- Schedule -------------------------------------------------------------------


def test_an_accepts_rule_needs_a_schedule() -> None:
    page_types = {"listing": {}, "product": {"record": "product", "fields": PRICE_FIELDS}}

    assert _errors(page_types, accepts=ACCEPTS) == [
        "accepts: a Site with an Accepts Rule needs a `schedule`"
    ]


def test_a_schedule_needs_an_accepts_rule() -> None:
    assert _errors({"listing": {}}, schedule={"every": "30m"}) == [
        "schedule: a Site with a Schedule needs `accepts`; "
        "the Dispatcher only schedules Supplied Start Requests"
    ]


def test_a_paused_schedule_is_valid() -> None:
    assert _accepting(schedule={"every": "1h", "enabled": False}) == []


def test_a_schedule_below_the_global_minimum_is_an_error() -> None:
    assert _accepting(schedule={"every": "5m"}) == [
        "schedule.every: `5m` is below the minimum of `15m` (`schedule.min_every` in defaults.yaml)"
    ]


def test_a_schedule_at_the_global_minimum_is_valid() -> None:
    assert _accepting(schedule={"every": "15m"}) == []


@pytest.mark.parametrize("every", ["30", "30 minutes", "1.5h", "0m", "m"])
def test_a_schedule_interval_is_a_whole_number_of_units(every: str) -> None:
    assert _accepting(schedule={"every": every}) == [
        f"schedule.every: `{every}` is not a duration like `30m`, `2h` or `1d`"
    ]


# --- Price Record contract -------------------------------------------------------


def test_the_accepted_page_type_must_emit_records() -> None:
    page_types = {"listing": {}, "product": {"fields": PRICE_FIELDS}}

    assert _errors(page_types, accepts=ACCEPTS, schedule={"every": "30m"}) == [
        "accepts.page_type: Page Type `product` emits no Records; "
        "an accepting Site's Records must match the Price Record contract"
    ]


def test_a_price_record_needs_the_required_fields() -> None:
    fields = {k: v for k, v in PRICE_FIELDS.items() if k != "price"}

    assert _accepting(fields=fields) == [
        "page_types.product.fields.price: missing; the Price Record contract requires it"
    ]


def test_price_record_fields_must_be_required_when_the_contract_requires_them() -> None:
    fields = {**PRICE_FIELDS, "price": {**PRICE_FIELDS["price"], "required": False}}

    assert _accepting(fields=fields) == [
        "page_types.product.fields.price: the Price Record contract requires `required: true`"
    ]


def test_price_record_fields_must_have_the_contract_type() -> None:
    fields = {**PRICE_FIELDS, "price": {**PRICE_FIELDS["price"], "type": "string"}}

    assert _accepting(fields=fields) == [
        "page_types.product.fields.price: type `string`, "
        "but the Price Record contract says `number`"
    ]


def test_optional_price_record_fields_are_type_checked_when_present() -> None:
    fields = {
        **PRICE_FIELDS,
        "is_deal": {"css": ".deal::text", "type": "string"},
        "unit_price_text": {"css": ".unit::text"},
    }

    assert _accepting(fields=fields) == [
        "page_types.product.fields.is_deal: type `string`, "
        "but the Price Record contract says `boolean`",
        "page_types.product.fields.unit_price_text: "
        "no type; add `type: string` to match the Price Record contract",
    ]


def test_a_price_record_may_have_every_optional_field_and_others() -> None:
    fields = {
        **PRICE_FIELDS,
        **{
            name: {"css": f".{name}::text", "type": "string"}
            for name in (
                "brand",
                "size",
                "unit_basis",
                "unit_price_text",
                "price_kind",
                "availability",
                "promo_text",
                "currency",
            )
        },
        **{
            name: {"css": f".{name}::text", "type": "number"}
            for name in ("regular_price", "unit_price")
        },
        **{
            name: {"css": f".{name}::text", "type": "boolean"}
            for name in ("is_deal", "store_verified")
        },
    }

    assert _accepting(fields=fields) == []


# --- Location labels -------------------------------------------------------------


def test_a_location_label_is_display_text() -> None:
    locations = {"syd": {"label": "Sydney G412"}, "melb": {"label": 3000}}

    with pytest.raises(ConfigError) as exc:
        _check({"listing": {}}, locations=locations)

    assert exc.value.errors == ["locations: Location `melb` has a non-string `label`"]


# --- Sites claiming one another's URLs ---------------------------------------------


def _accepting_site(name: str, url: str, *examples: str, retailer: str | None = None) -> Site:
    data = {
        "site": name,
        "start": [{"url": examples[0], "page_type": "product"}],
        "records": {"product": {}},
        "schedule": {"every": "30m"},
        "accepts": {
            "page_type": "product",
            "retailer": {"key": retailer or name, "name": name.upper()},
            "url": url,
            "examples": list(examples),
        },
        "page_types": {"product": {"record": "product", "fields": PRICE_FIELDS}},
    }
    return parse_site(data, DEFAULTS)


def test_sites_must_not_accept_one_anothers_examples() -> None:
    sites = {
        "a.yaml": _accepting_site("a", "^https://x\\.example/", "https://x.example/p/1"),
        "b.yaml": _accepting_site("b", "^https://x\\.example/p/", "https://x.example/p/2"),
        "c.yaml": _accepting_site("c", "^https://y\\.example/", "https://y.example/p/1"),
    }

    assert check_sites(sites) == [
        "a.yaml: accepts.examples[0]: `https://x.example/p/1` "
        "is also accepted by Site `b` (b.yaml)",
        "b.yaml: accepts.examples[0]: `https://x.example/p/2` "
        "is also accepted by Site `a` (a.yaml)",
    ]


def test_two_accepting_sites_must_not_share_a_retailer() -> None:
    sites = {
        "a.yaml": _accepting_site(
            "a", "^https://a\\.example/", "https://a.example/1", retailer="shop"
        ),
        "b.yaml": _accepting_site(
            "b", "^https://b\\.example/", "https://b.example/1", retailer="shop"
        ),
        "c.yaml": _accepting_site("c", "^https://c\\.example/", "https://c.example/1"),
    }

    assert check_sites(sites) == [
        "b.yaml: accepts.retailer: `shop` is also the retailer of Site `a` (a.yaml)"
    ]


def test_every_page_type_emitting_the_accepted_record_type_is_checked() -> None:
    page_types = {
        "listing": {"record": "product", "fields": {"name": {"css": "a::text", "type": "string"}}},
        "product": {"record": "product", "fields": PRICE_FIELDS},
    }
    errors = _errors(page_types, accepts=ACCEPTS, schedule={"every": "30m"})

    assert errors == [
        "page_types.listing.fields.url: missing; the Price Record contract requires it",
        "page_types.listing.fields.name: the Price Record contract requires `required: true`",
        "page_types.listing.fields.price: missing; the Price Record contract requires it",
    ]


@pytest.mark.parametrize(
    ("schedule", "error"),
    [
        ("15m", "defaults schedule: expected a mapping with `min_every`"),
        ({"min_every": "soon"}, "defaults schedule.min_every: `soon` is not a duration like "),
    ],
)
def test_malformed_schedule_defaults_are_a_config_error(schedule: Any, error: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_site({"site": "s"}, {**DEFAULTS, "schedule": schedule})

    [message] = exc.value.errors
    assert message.startswith(error)
