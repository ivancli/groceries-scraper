import json
from html import escape
from pathlib import Path
from urllib.parse import quote

import pytest
from parsel import Selector

from groceries_scraper.search.coles import Discovery, build_request, capability, parse_response

FIXTURES = Path(__file__).parent / "fixtures" / "coles-search"


def replay(body: bytes, query: str = "milk", status: int = 200) -> Discovery:
    return parse_response(
        build_request(query),
        body,
        status=status,
        response_url=f"https://www.coles.com.au/search/products?q={quote(query, safe='')}",
    )


def changed_search(**changes: object) -> bytes:
    body = (FIXTURES / "milk.html").read_text()
    source = Selector(body).css("script#__NEXT_DATA__::text").get()
    assert source is not None
    data = json.loads(source)
    data["props"]["pageProps"]["searchResults"].update(changes)
    return body.replace(source, json.dumps(data)).encode()


def test_retained_search_keeps_primary_organic_order_and_retailer_issued_metadata() -> None:
    discovery = parse_response(
        build_request("milk"),
        (FIXTURES / "milk.html").read_bytes(),
        status=200,
        response_url="https://www.coles.com.au/search/products?q=milk",
    )

    assert discovery.status == "ok"
    assert [r["retailer_product_id"] for r in discovery.results] == [
        "8150288",
        "9760047",
        "9759944",
        "4182384",
        "439693",
        "9760070",
        "9760091",
        "3186175",
        "3070829",
        "5458243",
    ]
    assert discovery.results[0] == {
        "retailer_product_id": "8150288",
        "title": "Coles Full Cream Milk | 3L",
        "product_url": "https://www.coles.com.au/product/coles-full-cream-milk-3l-8150288",
        "image_url": "https://www.coles.com.au/_next/image?url=https%3A%2F%2Fcdn.productimages.coles.com.au%2Fproductimages%2F8%2F8150288.jpg&w=640&q=90",
        "metadata": {"name": "Full Cream Milk", "brand": "Coles", "size_text": "3L"},
    }


def test_duplicate_primary_tiles_are_deduplicated_before_taking_ten() -> None:
    data = json.loads(
        Selector((FIXTURES / "milk.html").read_text()).css("script#__NEXT_DATA__::text").get() or ""
    )
    products = data["props"]["pageProps"]["searchResults"]["results"]
    products.insert(1, products[0])
    discovery = parse_response(
        build_request("milk"),
        changed_search(results=products),
        status=200,
        response_url="https://www.coles.com.au/search/products?q=milk",
    )
    assert len(discovery.results) == 10
    assert discovery.results[-1]["retailer_product_id"] == "5458243"


@pytest.mark.parametrize(
    "status,body",
    [
        (200, (FIXTURES / "challenge.html").read_bytes()),
        (403, b"Access Denied"),
        (429, b"Too many requests"),
        (200, b"<html><title>Just a moment...</title></html>"),
    ],
)
def test_refusal_and_challenge_are_blocked_instead_of_empty(status: int, body: bytes) -> None:
    discovery = replay(body, status=status)
    assert discovery.status == "blocked"
    assert discovery.results == []


@pytest.mark.parametrize(
    "body,status",
    [
        (b"<html><h1>No results</h1></html>", 200),
        (b'<script id="__NEXT_DATA__">{broken}</script>', 200),
        ((FIXTURES / "milk.html").read_bytes(), 503),
        (changed_search(keyword="bread"), 200),
        (changed_search(noOfResults=0), 200),
        (changed_search(results=[]), 200),
        (changed_search(start=48), 200),
        (changed_search(alternateResult=True), 200),
    ],
    ids=["missing", "json", "http", "query", "count", "products", "page", "alternate"],
)
def test_unverified_and_inconsistent_search_responses_fail(body: bytes, status: int) -> None:
    discovery = replay(body, status=status)
    assert discovery.status == "failed"
    assert discovery.results == []


def test_simulated_empty_requires_valid_query_membership_and_zero_retailer_count() -> None:
    # Protocol fixture only: this is deliberately not a genuine empty retailer capture.
    body = changed_search(noOfResults=0, results=[])
    page = Selector(body.decode())
    for node in page.css('section[data-testid="product-tile"]'):
        node.root.getparent().remove(node.root)
    discovery = replay(page.get().encode())
    assert discovery.status == "ok"
    assert discovery.results == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", True),
        ("id", "different"),
        ("_type", "UNKNOWN"),
        ("featured", "false"),
        ("name", 42),
        ("brand", "x" * 201),
        ("size", "bad\x00size"),
    ],
)
def test_corrupt_product_fields_fail_even_beyond_the_limit(field: str, value: object) -> None:
    source = Selector((FIXTURES / "milk.html").read_text()).css("script#__NEXT_DATA__::text").get()
    data = json.loads(source or "")
    products = data["props"]["pageProps"]["searchResults"]["results"]
    products[-1][field] = value
    discovery = replay(changed_search(results=products))
    assert discovery.status == "failed"
    assert discovery.results == []


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/product/coles-full-cream-milk-3l-8150288",
        "//evil.example/product/coles-full-cream-milk-3l-8150288",
        "http://www.coles.com.au/product/coles-full-cream-milk-3l-8150288",
        "https://user@www.coles.com.au/product/coles-full-cream-milk-3l-8150288",
        "https://www.coles.com.au:444/product/coles-full-cream-milk-3l-8150288",
        "/product/../coles-full-cream-milk-3l-8150288",
        "/product/%2e%2e/coles-full-cream-milk-3l-8150288",
        "/product/x%2fcoles-full-cream-milk-3l-8150288",
        "/product/coles-full-cream-milk-3l-8150288#reviews",
        "/product/coles-full-cream-milk-3l-8150288?bad=white space",
    ],
)
def test_invalid_required_product_urls_fail_the_result(href: str) -> None:
    body = (
        (FIXTURES / "milk.html")
        .read_text()
        .replace(
            "/product/coles-full-cream-milk-3l-8150288",
            href,
        )
    )
    assert replay(body.encode()).status == "failed"


@pytest.mark.parametrize("replacement", ["", "x" * 513])
def test_missing_or_oversized_display_title_fails(replacement: str) -> None:
    body = (
        (FIXTURES / "milk.html")
        .read_text()
        .replace(
            "Coles Full Cream Milk | 3L",
            replacement,
        )
    )
    assert replay(body.encode()).status == "failed"


@pytest.mark.parametrize(
    "image",
    [
        None,
        "https://evil.example/productimages/milk.jpg",
        "/_next/image?url=https%3A%2F%2Fevil.example%2Fmilk.jpg&w=640",
        "/_next/image?url=https%3A%2F%2Fcdn.productimages.coles.com.au%2Fproductimages%2F8%2F8150288.jpg&url=https%3A%2F%2Fevil.example%2Fx",
        "/_next/image?url=%2Fproductimages%2F8%2F8150288.jpg",
        "/_next/other?url=https%3A%2F%2Fcdn.productimages.coles.com.au%2Fproductimages%2F8%2F8150288.jpg",
        "https://cdn.productimages.coles.com.au/other/milk.jpg",
        "https://cdn.productimages.coles.com.au/productimages/%2e%2e/milk.jpg",
    ],
    ids=[
        "missing",
        "host",
        "embedded-host",
        "duplicate-source",
        "relative-source",
        "optimizer-path",
        "cdn-path",
        "encoded-traversal",
    ],
)
def test_invalid_or_missing_images_leave_the_product_selectable(image: str | None) -> None:
    body = (FIXTURES / "milk.html").read_text()
    source = Selector(body).css('img[data-testid="product-image"]::attr(src)').get()
    assert source is not None
    body = body.replace(escape(source, quote=True), escape(image or "", quote=True), 1)
    discovery = replay(body.encode())
    assert discovery.status == "ok"
    assert discovery.results[0]["image_url"] is None
    assert discovery.results[0]["retailer_product_id"] == "8150288"


def test_a_valid_retailer_issued_cdn_image_is_retained_without_guessing_a_path() -> None:
    body = (FIXTURES / "milk.html").read_text()
    source = Selector(body).css('img[data-testid="product-image"]::attr(src)').get()
    assert source is not None
    body = body.replace(
        escape(source, quote=True),
        "https://cdn.productimages.coles.com.au/productimages/custom/milk.jpg",
        1,
    )
    assert replay(body.encode()).results[0]["image_url"] == (
        "https://cdn.productimages.coles.com.au/productimages/custom/milk.jpg"
    )


def test_query_is_encoded_once_and_uses_the_existing_configured_identity_and_policy() -> None:
    request = build_request("  a2 Milk  3L & café+? ")
    assert request.query == "  a2 Milk  3L & café+? "
    assert request.url == (
        "https://www.coles.com.au/search?q=%20%20a2%20Milk%20%203L%20%26%20caf%C3%A9%2B%3F%20"
    )
    assert request.user_agent == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )
    assert request.obey_robots


@pytest.mark.parametrize("query", ["", "  ", "x" * 201, "milk\x00", "milk\x7f"])
def test_invalid_queries_cannot_construct_a_request(query: str) -> None:
    with pytest.raises(ValueError, match="invalid_query"):
        build_request(query)


@pytest.mark.parametrize(
    "response_url",
    [
        "https://evil.example/search/products?q=milk",
        "https://www.coles.com.au/product/milk-8150288",
        "https://www.coles.com.au/search/products?q=bread",
        "https://www.coles.com.au/search/products?q=milk&q=bread",
        "https://www.coles.com.au/search/products?q=milk&page=2",
    ],
)
def test_search_page_identity_must_match_the_request(response_url: str) -> None:
    discovery = parse_response(
        build_request("milk"),
        (FIXTURES / "milk.html").read_bytes(),
        status=200,
        response_url=response_url,
    )
    assert discovery.status == "failed"


def test_disabled_definition_discloses_online_default_without_claiming_home_store_context() -> None:
    definition = capability()
    assert definition["retailer"] == "coles"
    assert definition["adapter_version"] == "coles-live-v1"
    assert definition["enabled"] is False
    assert definition["context_mappings"] == []
    assert definition["default_context"] == {
        "kind": "online",
        "location": "default",
        "channel": "delivery",
        "label": "Coles online default (Home Store not applied; location unverified)",
        "home_store_applied": False,
    }
    discovery = replay((FIXTURES / "milk.html").read_bytes())
    assert discovery.context == definition["default_context"]
    assert definition["product_rules"] == [
        {"host": "www.coles.com.au", "path_prefix": "/product/"},
    ]
    assert definition["image_rules"] == [
        {"host": "cdn.productimages.coles.com.au", "path_prefix": "/productimages/"},
        {"host": "www.coles.com.au", "path_prefix": "/_next/", "source_param": "url"},
    ]


@pytest.mark.parametrize("query", ["Full Cream Milk", "a2 Milk", "a2 Milk 3L"])
def test_simulated_refinements_use_structured_membership_instead_of_title_substrings(
    query: str,
) -> None:
    # Changes only the query association, never claims a second genuine retailer capture.
    body = changed_search(keyword=query)
    assert replay(body, query=query).results[0]["title"] == "Coles Full Cream Milk | 3L"


def test_explicit_sponsored_label_is_excluded_even_without_structured_advertising_flags() -> None:
    body = (
        (FIXTURES / "milk.html")
        .read_text()
        .replace(
            '<section data-testid="product-tile">',
            '<section data-testid="product-tile"><ul class="product__top_messaging">'
            '<li class="product__top_messaging__item">Sponsored</li></ul>',
            1,
        )
    )
    assert replay(body.encode()).results[0]["retailer_product_id"] == "9760047"


def test_conflicting_urls_for_the_same_retailer_identity_fail_instead_of_guessing_equivalence() -> (
    None
):
    body = (FIXTURES / "milk.html").read_text()
    tile = Selector(body).css('section[data-testid="product-tile"]').get()
    assert tile is not None
    conflict = tile.replace("coles-full-cream-milk-3l-8150288", "other-slug-8150288")
    assert replay(body.replace(tile, tile + conflict).encode()).status == "failed"


@pytest.mark.parametrize("body", [b"", b"\xff<html>corrupt</html>"])
def test_empty_or_undecodable_documents_fail(body: bytes) -> None:
    assert replay(body).status == "failed"


def test_corrupt_encoding_in_required_title_cannot_become_successful_metadata() -> None:
    body = (
        (FIXTURES / "milk.html")
        .read_bytes()
        .replace(
            b"Coles Full Cream Milk | 3L",
            b"Coles \xff Full Cream Milk | 3L",
            1,
        )
    )
    assert replay(body).status == "failed"


def test_relative_url_cannot_exceed_the_contract_bound_after_resolution() -> None:
    body = (
        (FIXTURES / "milk.html")
        .read_text()
        .replace(
            "/product/coles-full-cream-milk-3l-8150288",
            "/product/" + "x" * 2030 + "-8150288",
        )
    )
    assert replay(body.encode()).status == "failed"


def test_individually_valid_fields_cannot_create_an_oversized_result_payload() -> None:
    page = Selector((FIXTURES / "milk.html").read_text())
    source = page.css("script#__NEXT_DATA__::text").get()
    data = json.loads(source or "")
    for product in data["props"]["pageProps"]["searchResults"]["results"]:
        for key in ("name", "brand", "size"):
            product[key] = "🍎" * 200
    for node in page.css("h2.product__title"):
        node.root.text = "🍎" * 512
    for image in page.css('img[data-testid="product-image"]'):
        image.root.set("src", "https://cdn.productimages.coles.com.au/productimages/" + "x" * 1980)
    page.css("script#__NEXT_DATA__")[0].root.text = json.dumps(data)
    assert replay(page.get().encode()).status == "failed"


@pytest.mark.parametrize("query", ["Just a moment", "access denied", "unusual traffic"])
def test_query_text_in_genuine_search_headings_is_not_a_challenge(query: str) -> None:
    body = (
        changed_search(keyword=query)
        .decode()
        .replace(
            '<h1 data-testid="page-header">Results for &quot;milk&quot;</h1>',
            f"<title>Search {query} | Coles</title><h1>Results for &quot;{query}&quot;</h1>"
            f'<h1 data-testid="page-header">Results for &quot;{query}&quot;</h1>',
        )
    )
    assert replay(body.encode(), query=query).status == "ok"
