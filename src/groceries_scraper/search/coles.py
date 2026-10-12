"""Coles discovery projection over a genuine search document; never performs price ingestion."""

import json
import re
from dataclasses import dataclass, field
from typing import Literal, TypedDict
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from parsel import Selector

ORIGIN = "https://www.coles.com.au"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PRODUCT_HOST = "www.coles.com.au"
PRODUCT_PATH = "/product/"
IMAGE_HOST = "cdn.productimages.coles.com.au"
IMAGE_PATH = "/productimages/"


def _default_context() -> dict[str, str | bool]:
    return {
        "kind": "online",
        "location": "default",
        "channel": "delivery",
        "label": "Coles online default (Home Store not applied; location unverified)",
        "home_store_applied": False,
    }


def capability() -> dict[str, object]:
    """The reviewed definition for future publication; launch evidence is still incomplete."""
    return {
        "retailer": "coles",
        "retailer_name": "Coles",
        "adapter_version": "coles-live-v1",
        "enabled": False,
        "mode": "live",
        "default_context": _default_context(),
        "context_mappings": [],
        "ranking_policy": "primary-organic-v1",
        "product_rules": [{"host": PRODUCT_HOST, "path_prefix": PRODUCT_PATH}],
        "image_rules": [
            {"host": IMAGE_HOST, "path_prefix": IMAGE_PATH},
            {"host": PRODUCT_HOST, "path_prefix": "/_next/", "source_param": "url"},
        ],
    }


class Candidate(TypedDict):
    retailer_product_id: str
    title: str
    product_url: str
    image_url: str | None
    metadata: dict[str, str | None]


@dataclass(frozen=True)
class SearchRequest:
    query: str
    url: str
    user_agent: str = USER_AGENT
    obey_robots: bool = True
    render: Literal["browser"] = "browser"


@dataclass(frozen=True)
class Discovery:
    status: Literal["ok", "blocked", "failed"]
    results: list[Candidate]
    error: str | None = None
    context: dict[str, str | bool] = field(default_factory=_default_context)


def build_request(query: str) -> SearchRequest:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 200
        or any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in query)
    ):
        raise ValueError("invalid_query")
    return SearchRequest(query, f"{ORIGIN}/search?q={quote(query, safe='')}")


def parse_response(
    request: SearchRequest, body: bytes, *, status: int, response_url: str
) -> Discovery:
    try:
        page = Selector(body.decode("utf-8"))
    except ValueError:
        return Discovery("failed", [], "undecodable_response")
    heading = " ".join(page.css("title,h1").xpath("string()").getall())
    search_layout = page.css('h1[data-testid="page-header"]') and page.css("script#__NEXT_DATA__")
    if (
        status in (401, 403, 429)
        or page.xpath('//iframe[contains(@src,"/_Incapsula_Resource")]')
        or (
            not search_layout
            and re.search(
                r"access denied|just a moment|verify.*human|unusual traffic", heading, re.I
            )
        )
    ):
        return Discovery("blocked", [], "retailer_blocked")
    if status != 200:
        return Discovery("failed", [], "http_error")
    validated = _url(response_url, "www.coles.com.au", "/search")
    if validated is None:
        return Discovery("failed", [], "unexpected_response_url")
    parts = urlsplit(validated)
    if parts.path not in ("/search", "/search/products") or parse_qs(
        parts.query, keep_blank_values=True
    ) != {"q": [request.query]}:
        return Discovery("failed", [], "unexpected_response_url")
    try:
        return _project(page, request)
    except (KeyError, TypeError, ValueError, AttributeError):
        return Discovery("failed", [], "corrupt_response")


def _project(page: Selector, request: SearchRequest) -> Discovery:
    data = json.loads(page.css("script#__NEXT_DATA__::text").get() or "null")
    search = data["props"]["pageProps"]["searchResults"]
    count, products = search["noOfResults"], search["results"]
    if (
        search["keyword"] != request.query
        or type(count) is not int
        or count < 0
        or type(search["start"]) is not int
        or search["start"] != 0
        or type(search["pageSize"]) is not int
        or not 0 < search["pageSize"] <= 100
        or search["alternateResult"] is not False
        or not isinstance(products, list)
        or not page.css('h1[data-testid="page-header"]')
        or (count == 0) != (len(products) == 0)
        or (count == 0 and page.css('section[data-testid="product-tile"]'))
    ):
        raise ValueError("unverified_search")
    tiles: dict[str, list[Selector]] = {}
    for tile in page.css('section[data-testid="product-tile"]'):
        href = tile.css("a::attr(href)").get() or ""
        identity = urlsplit(href).path.rsplit("-", 1)[-1]
        tiles.setdefault(identity, []).append(tile)
    results: list[Candidate] = []
    seen: dict[str, str] = {}
    for product in products:
        kind = product["_type"]
        if kind == "PRODUCT_ASSOCIATION":
            continue
        if (
            kind != "PRODUCT"
            or type(product["featured"]) is not bool
            or any(
                product[key] is not None and not isinstance(product[key], str)
                for key in ("adId", "adSource")
            )
        ):
            raise ValueError("corrupt_membership")
        if any(product[key] for key in ("adId", "adSource", "featured")):
            continue
        raw_id = product["id"]
        if type(raw_id) not in (str, int) or not re.fullmatch(r"[0-9]{1,128}", str(raw_id)):
            raise ValueError("corrupt_identity")
        identity = str(raw_id)
        matched = tiles[identity]
        if any(
            tile.xpath(
                './/li[contains(concat(" ",normalize-space(@class)," "),'
                '" product__top_messaging__item ")][normalize-space(.)="Sponsored"]'
            )
            for tile in matched
        ):
            continue
        links = {
            _url(href, PRODUCT_HOST, PRODUCT_PATH)
            for tile in matched
            for href in tile.css("a::attr(href)").getall()
        }
        if len(links) != 1 or None in links:
            raise ValueError("unresolved_product_url")
        product_url = links.pop()
        assert product_url is not None
        if not urlsplit(product_url).path.endswith(f"-{identity}"):
            raise ValueError("inconsistent_identity")
        tile = matched[0]
        title = _display(tile.css("h2.product__title").xpath("string()").get(), 512)
        if title is None:
            raise ValueError("missing_title")
        metadata = {
            "name": _display(product.get("name"), 200),
            "brand": _display(product.get("brand"), 200),
            "size_text": _display(product.get("size"), 200),
        }
        if identity in seen:
            if seen[identity] != product_url:
                raise ValueError("inconsistent_identity")
            continue
        seen[identity] = product_url
        image = tile.css('img[data-testid="product-image"]::attr(src)').get()
        results.append(
            {
                "retailer_product_id": identity,
                "title": title,
                "product_url": product_url,
                "image_url": _image(image),
                "metadata": metadata,
            }
        )
    discovery = Discovery("ok", results[:10])
    payload = {"context": discovery.context, "results": discovery.results}
    # Leave space for the service's identity, source time and terminal framing.
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 60 * 1024:
        raise ValueError("oversize_discovery")
    return discovery


def _display(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("corrupt_display_text")
    text = " ".join(value.split())
    if not text or len(text) > maximum:
        raise ValueError("corrupt_display_text")
    return text


def _url(value: str, host: str, prefix: str) -> str | None:
    if len(value) > 2048 or re.search(r'[\s\\<>"\x00-\x1f\x7f]', value):
        return None
    try:
        raw = urlsplit(value)
        if any(part in (".", "..") for part in raw.path.split("/")) or re.search(
            r"%(?:2f|5c|2e)", raw.path, re.I
        ):
            return None
        url = urlsplit(urljoin(ORIGIN, value))
        if (
            url.scheme != "https"
            or url.hostname != host
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or url.fragment
            or not url.path.startswith(prefix)
        ):
            return None
        resolved = urlunsplit(("https", host, url.path, url.query, ""))
        return resolved if len(resolved) <= 2048 else None
    except ValueError:
        return None


def _image(value: str | None) -> str | None:
    if not value:
        return None
    cdn = _url(value, IMAGE_HOST, IMAGE_PATH)
    if cdn is not None:
        return cdn
    optimizer = _url(value, PRODUCT_HOST, "/_next/")
    if optimizer is None or urlsplit(optimizer).path != "/_next/image":
        return None
    sources = parse_qs(urlsplit(optimizer).query, keep_blank_values=True).get("url", [])
    if (
        len(sources) != 1
        or not sources[0].startswith("https://")
        or _url(sources[0], IMAGE_HOST, IMAGE_PATH) is None
    ):
        return None
    return optimizer
