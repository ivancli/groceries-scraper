from datetime import timedelta
from pathlib import Path

import pytest

from groceries_scraper.config import load_checked_site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "sites" / "jbhifi_picks.yaml"
URL = "https://www.jbhifi.com.au/products/apple-airpods-4"


def test_jbhifi_picks_accepts_product_urls_every_30_minutes() -> None:
    site, _ = load_checked_site(SITE)

    assert site.schedule is not None and site.schedule.every == timedelta(minutes=30)
    assert site.accepts is not None
    assert site.accepts.retailer.key == "jbhifi"
    assert site.accepts.matches(URL)
    assert not site.accepts.matches(
        "https://www.jbhifi.com.au/collections/headphones-speakers-audio"
    )


def _product_page(availability: str) -> bytes:
    return f"""
    <link rel='canonical' href='{URL}'>
    <script type="application/ld+json">
      {{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}}
    </script>
    <script type="application/ld+json">
      {{"@context": "https://schema.org", "@type": "Product", "url": "{URL}",
        "name": "Apple AirPods 4",
        "brand": {{"@type": "Brand", "name": "APPLE"}},
        "offers": {{"@type": "Offer", "availability": "{availability}", "price": "219.00",
          "priceSpecification": [
            {{"priceType": "https://schema.org/ListPrice", "price": "219.00"}}
          ],
          "priceCurrency": "AUD"}}}}
    </script>
    """.encode()


def test_jbhifi_product_reads_fields_from_json_ld() -> None:
    site, _ = load_checked_site(SITE)

    result = evaluate_response(
        site.page_types["product"],
        _product_page("https://schema.org/InStock"),
        "text/html",
        PipeContext(url=URL),
    )

    assert result.extraction.dropped == []
    assert result.extraction.records[0].data == {
        "url": URL,
        "name": "Apple AirPods 4",
        "brand": "APPLE",
        "price": 219.0,
        "currency": "AUD",
        "availability": "in_stock",
    }


# The tracker 400s a whole delivery over one availability it doesn't know, so others are left out.
@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        ("https://schema.org/InStock", "in_stock"),
        ("https://schema.org/OutOfStock", "out_of_stock"),
        ("http://schema.org/OutOfStock", "out_of_stock"),
        ("https://schema.org/ComingSoon", None),
        ("https://schema.org/PreOrder", None),
    ],
)
def test_jbhifi_product_sends_only_availability_the_tracker_accepts(
    availability: str, expected: str | None
) -> None:
    site, _ = load_checked_site(SITE)

    result = evaluate_response(
        site.page_types["product"], _product_page(availability), "text/html", PipeContext(url=URL)
    )

    assert result.extraction.records[0].data.get("availability") == expected
