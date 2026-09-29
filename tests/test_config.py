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
    assert [step.kind for step in fields["unit_price"].pipe] == ["jsonpath", "fn"]


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
