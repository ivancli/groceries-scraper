from datetime import timedelta
from pathlib import Path

import pytest

from groceries_scraper.config import load_checked_site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.aldi.com.au/products",
        "https://www.aldi.com.au/products/drinks/k/1000000000",
    ],
)
def test_aldi_listing_rules_extract_products_and_follow_pagination(url: str) -> None:
    site, _ = load_checked_site(ROOT / "sites" / "aldi.yaml")
    body = b"""
    <a class="product-tile__link" href="/product/coffee-machine-123">
      <div data-test="product-tile__name"><p>Coffee Machine</p></div>
      <div data-test="product-tile__price">
        <span class="base-price__regular"><span>$1,299.00</span></span>
      </div>
    </a>
    <a data-test="next-page" href="?page=2">Next</a>
    <a href="/recipes">Recipes</a>
    """
    result = evaluate_response(site.page_types["listing"], body, "text/html", PipeContext(url=url))

    assert len(result.extraction.records) == 1 and result.extraction.dropped == []
    record = result.extraction.records[0].data
    assert record["name"] == "Coffee Machine"
    assert record["price"] == 1299.0
    assert record["currency"] == "AUD"
    assert record["url"] == "https://www.aldi.com.au/product/coffee-machine-123"
    assert [request.request.url for request in result.follow.requests] == [f"{url}?page=2"]


def test_aldi_listing_stops_when_there_is_no_next_page() -> None:
    site, _ = load_checked_site(ROOT / "sites" / "aldi.yaml")
    result = evaluate_response(
        site.page_types["listing"],
        b'<a href="/products?page=1">Previous</a><a href="/products?page=2">2</a>',
        "text/html",
        PipeContext(url="https://www.aldi.com.au/products?page=3"),
    )
    assert result.follow.requests == []


def test_aldi_picks_accepts_product_urls_every_30_minutes() -> None:
    site, _ = load_checked_site(ROOT / "sites" / "aldi_picks.yaml")

    assert site.schedule is not None and site.schedule.every == timedelta(minutes=30)
    assert site.accepts is not None
    assert site.accepts.matches("https://www.aldi.com.au/product/x-000000000000282420")
    assert not site.accepts.matches("https://www.aldi.com.au/products")
    assert "unit_price_text" in site.page_types["product"].fields
