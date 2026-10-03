import re
from pathlib import Path
from typing import Any

import pytest

from groceries_scraper.config import ConfigError, load_site, parse_site

ROOT = Path(__file__).parents[1]

DEFAULTS = {
    "download_delay": 1.0,
    "concurrent_requests_per_domain": 2,
    "obey_robots": True,
    "record_level": "all",
}


def _design_example() -> str:
    design = (ROOT / "docs" / "design.md").read_text()
    match = re.search(r"## Site config — full example\s+```yaml\n(.*?)```", design, re.S)
    assert match
    return match.group(1)


def test_full_design_example_parses(tmp_path: Path) -> None:
    site_file = tmp_path / "example.yaml"
    site_file.write_text(_design_example())

    site = load_site(site_file, defaults_path=ROOT / "defaults.yaml")

    assert site.site == "example_grocer"
    assert site.start[0].page_type == "listing"
    tile_rule = site.page_types["listing"].follow[0]
    assert tile_rule.scope == "each"
    assert [step.kind for step in tile_rule.pass_["price"]] == ["css", "regex", "replace"]
    assert tile_rule.pass_["category"][0].absolute is True
    assert tile_rule.request is not None
    assert tile_rule.request.json_body == {"sku": "{{ sku }}"}
    fields = site.page_types["product_api"].fields
    assert fields["sku"].required is True
    assert fields["sku"].pipe[0].var == "sku"
    assert fields["images"].items is not None
    assert fields["nutrition"].fields["kcal"].type == "integer"
    assert fields["variants"].each[0].jsonpath == "$.variants[*]"
    assert [step.kind for step in fields["unit_price"].pipe] == ["jsonpath", "regex", "replace"]


def _site(**sections: Any) -> dict[str, Any]:
    return {
        "site": "s",
        "start": [{"url": "https://x.example/", "page_type": "listing"}],
        "page_types": {"listing": {}},
        **sections,
    }


def _errors(data: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        parse_site(data, DEFAULTS)
    return exc.value.errors


def test_page_types_render_over_http_unless_they_opt_into_a_browser() -> None:
    site = parse_site(_site(page_types={"listing": {}, "app": {"render": "browser"}}), DEFAULTS)

    assert not site.page_types["listing"].renders_in_browser
    assert site.page_types["app"].renders_in_browser


def test_site_settings_override_defaults() -> None:
    site = parse_site(_site(settings={"download_delay": 3.5}), DEFAULTS)

    assert site.settings.download_delay == 3.5
    assert site.settings.concurrent_requests_per_domain == 2


def test_non_allowlisted_setting_is_rejected() -> None:
    errors = _errors(_site(settings={"user_agent": "bot"}))

    assert errors == ["settings.user_agent: Extra inputs are not permitted"]


def _product_field(field: Any) -> dict[str, Any]:
    return _site(page_types={"product": {"record": "product", "fields": {"f": field}}})


def _tile_pass(pipe: Any) -> dict[str, Any]:
    rule = {"select": {"css": "a::attr(href)"}, "page_type": "listing", "pass": {"sku": pipe}}
    return _site(page_types={"listing": {"follow": [rule]}})


A_PIPE = {"jsonpath": "$.x"}
ONE_KIND = "a Step needs exactly one of: css, xpath"
A_RULE = {"select": {"css": "a::attr(href)"}, "page_type": "listing"}


@pytest.mark.parametrize(
    ("data", "path", "message"),
    [
        pytest.param(
            _product_field({"css": "b", "requird": True}),
            "page_types.product.fields.f.requird",
            "Extra inputs are not permitted",
            id="unknown-field-key",
        ),
        pytest.param(
            _tile_pass([{"css": "b"}, {"regx": "\\d+"}]),
            "page_types.listing.follow[0].pass.sku[1].regx",
            "Extra inputs are not permitted",
            id="unknown-key-in-pipe-list",
        ),
        pytest.param(
            _site(health={"max_dropped_ratio": "lots"}),
            "health.max_dropped_ratio",
            "Input should be a valid number",
            id="bad-type",
        ),
        pytest.param(
            _product_field({"type": "array", **A_PIPE, "items": {}, "each": A_PIPE, "fields": {}}),
            "page_types.product.fields.f",
            "an array Field needs exactly one of `items` or `each`",
            id="array-items-and-each",
        ),
        pytest.param(
            _product_field({"type": "array", **A_PIPE}),
            "page_types.product.fields.f",
            "an array Field needs exactly one of `items` or `each`",
            id="array-neither-items-nor-each",
        ),
        pytest.param(
            _product_field({"type": "array", "each": A_PIPE}),
            "page_types.product.fields.f",
            "an array Field with `each` needs `fields`",
            id="array-each-without-fields",
        ),
        *[
            pytest.param(
                _product_field({"type": "array", **A_PIPE, "items": items}),
                "page_types.product.fields.f",
                "an array Field's `items` only takes a scalar `type`",
                id=f"array-items-with-{name}",
            )
            for name, items in [
                ("default", {"type": "integer", "default": 0}),
                ("required", {"required": True}),
                ("pipe", {"type": "string", **A_PIPE}),
                ("object-type", {"type": "object", "fields": {"g": A_PIPE}}),
            ]
        ],
        pytest.param(
            _product_field({"type": "object", **A_PIPE}),
            "page_types.product.fields.f",
            "an object Field needs `fields`",
            id="object-without-fields",
        ),
        pytest.param(
            _tile_pass({"css": "b", "xpath": "//b"}),
            "page_types.listing.follow[0].pass.sku",
            ONE_KIND,
            id="step-with-two-kinds",
        ),
        pytest.param(
            _tile_pass([{"css": "b"}, {"absolute": True}]),
            "page_types.listing.follow[0].pass.sku[1]",
            ONE_KIND,
            id="step-without-kind",
        ),
        pytest.param(
            _site(page_types={"listing": {"follow": [{**A_RULE, "scope": "each"}]}}),
            "page_types.listing",
            "a Follow Rule with `scope: each` needs a page-level `items` Loop",
            id="each-scope-without-loop",
        ),
        pytest.param(
            _site(page_types={"listing": {"follow": [{**A_RULE, "as": "session"}]}}),
            "page_types.listing.follow[0]",
            "`as` and `pass` cannot use reserved template names: session",
            id="as-reserved-name",
        ),
        pytest.param(
            _site(
                page_types={
                    "listing": {"follow": [{**A_RULE, "pass": {"env": A_PIPE, "x": A_PIPE}}]}
                }
            ),
            "page_types.listing.follow[0]",
            "`as` and `pass` cannot use reserved template names: env",
            id="pass-reserved-name",
        ),
        pytest.param(
            _site(
                page_types={
                    "listing": {"follow": [{**A_RULE, "request": {"form": {}, "body": "x"}}]}
                }
            ),
            "page_types.listing.follow[0].request",
            "a Request Template takes one of `json`, `form` or `body`",
            id="two-request-bodies",
        ),
        pytest.param(
            _site(page_types={"listing": {"response": "json", "render": "browser"}}),
            "page_types.listing",
            "`render: browser` needs `response: html`",
            id="browser-render-of-json",
        ),
        pytest.param(
            _site(page_types={"listing": {"render": "chrome"}}),
            "page_types.listing.render",
            "Input should be 'http' or 'browser'",
            id="unknown-render",
        ),
        pytest.param(
            _site(records={"product": {"fields": {}}}),
            "records.product.fields",
            "Dictionary should have at least 1 item",
            id="empty-contract",
        ),
        pytest.param(
            _site(records={"product": {"fields": {"sku": {"required": True}}}}),
            "records.product.fields.sku.type",
            "Field required",
            id="untyped-contract-field",
        ),
        pytest.param(
            _site(records={"product": {"fields": {"tags": {"type": "array"}}}}),
            "records.product.fields.tags",
            "an array Field needs exactly one of `items` or `fields`",
            id="contract-array-without-shape",
        ),
        pytest.param(
            _site(
                records={"p": {"fields": {"n": {"type": "object", "items": {"type": "string"}}}}}
            ),
            "records.p.fields.n",
            "an object Field needs `fields`",
            id="contract-object-without-fields",
        ),
        pytest.param(
            _site(
                records={"p": {"fields": {"n": {"type": "string", "items": {"type": "string"}}}}}
            ),
            "records.p.fields.n",
            "`fields` and `items` need type object or array",
            id="contract-scalar-with-items",
        ),
    ],
)
def test_invalid_config_reports_yaml_path(data: dict[str, Any], path: str, message: str) -> None:
    errors = _errors(data)

    assert len(errors) == 1
    assert errors[0].startswith(f"{path}: {message}")


def test_empty_pipe_is_rejected() -> None:
    errors = _errors(_tile_pass([]))

    assert errors == [
        "page_types.listing.follow[0].pass.sku: "
        "List should have at least 1 item after validation, not 0"
    ]


def test_defaults_are_found_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_file = tmp_path / "example.yaml"
    site_file.write_text(_design_example())
    monkeypatch.chdir(tmp_path)

    site = load_site(site_file)

    assert site.settings.record_level == "all"
