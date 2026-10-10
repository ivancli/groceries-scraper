from datetime import timedelta
from pathlib import Path

import pytest

from groceries_scraper.config import load_checked_site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "sites" / "woolworths_picks.yaml"
URL = (
    "https://www.woolworths.com.au/shop/productdetails/151299/"
    "dettol-antibacterial-disinfectant-liquid-solution"
)
BODY = (ROOT / "tests" / "fixtures" / "woolworths-product.html").read_bytes()


def test_woolworths_picks_accepts_product_urls_every_30_minutes() -> None:
    site, _ = load_checked_site(SITE)
    assert site.schedule is not None and site.schedule.every == timedelta(minutes=30)
    assert site.accepts is not None
    assert (site.accepts.retailer.key, site.accepts.retailer.name) == ("woolworths", "Woolworths")
    assert site.accepts.matches(URL)
    assert not site.accepts.matches("https://www.woolworths.com.au/shop/browse/cleaning")
    assert not site.accepts.matches(URL.replace("151299", "invalid"))


def test_woolworths_extracts_captured_product() -> None:
    site, _ = load_checked_site(SITE)
    result = evaluate_response(site.page_types["product"], BODY, "text/html", PipeContext(url=URL))
    assert result.extraction.dropped == []
    assert result.extraction.records[0].data == {
        "url": URL,
        "name": "Dettol Antibacterial Disinfectant Liquid Solution 2L",
        "brand": "Dettol",
        "price": 24.0,
        "currency": "AUD",
        "availability": "in_stock",
        "size": "2L",
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [("https://schema.org/OutOfStock", "out_of_stock"), ("http://schema.org/PreOrder", None)],
)
def test_woolworths_normalizes_availability(status: str, expected: str | None) -> None:
    site, _ = load_checked_site(SITE)
    body = BODY.replace(b"http://schema.org/InStock", status.encode())
    result = evaluate_response(site.page_types["product"], body, "text/html", PipeContext(url=URL))
    assert result.extraction.dropped == []
    assert result.extraction.records[0].data["availability"] == expected


def test_woolworths_missing_price_drops_product() -> None:
    site, _ = load_checked_site(SITE)
    body = BODY.replace(b'"price":24,', b"")
    result = evaluate_response(site.page_types["product"], body, "text/html", PipeContext(url=URL))
    assert result.extraction.records == []
    assert len(result.extraction.dropped) == 1
