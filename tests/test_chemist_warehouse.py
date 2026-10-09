from datetime import timedelta
from pathlib import Path

from groceries_scraper.config import load_checked_site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "sites" / "chemist_warehouse_picks.yaml"
URL = "https://www.chemistwarehouse.com.au/buy/102414/aptagrow-nutrient-dense-milk-drink-from-3-years-900g"


def test_chemist_warehouse_picks_accepts_product_urls_every_30_minutes() -> None:
    site, _ = load_checked_site(SITE)

    assert site.schedule is not None and site.schedule.every == timedelta(minutes=30)
    assert site.accepts is not None
    assert site.accepts.matches(URL)
    assert not site.accepts.matches("https://www.chemistwarehouse.com.au/shop-online/258/medicines")


def test_chemist_warehouse_product_reads_price_from_next_data() -> None:
    site, _ = load_checked_site(SITE)
    body = f"""
    <link rel="canonical" href="{URL}">
    <h1>AptaGrow Nutrient-Dense Milk Drink From 3+ Years 900g</h1>
    <script id="__NEXT_DATA__" type="application/json">
      {{"props": {{"pageProps": {{"product": {{
        "product": {{"variants": [{{"brand": {{"key": "aptamil", "label": "Aptamil"}}}}]}},
        "prices": [{{"price": {{
          "value": {{"amount": 39.99, "currencyCode": "AUD"}},
          "rrp": {{"amount": 45.99, "currencyCode": "AUD"}}
        }}}}],
        "availability": [{{"sku": "2694907", "status": "in-stock"}}]
      }}}}}}}}
    </script>
    """.encode()

    result = evaluate_response(site.page_types["product"], body, "text/html", PipeContext(url=URL))

    assert result.extraction.dropped == []
    assert result.extraction.records[0].data == {
        "url": URL,
        "name": "AptaGrow Nutrient-Dense Milk Drink From 3+ Years 900g",
        "brand": "Aptamil",
        "price": 39.99,
        "currency": "AUD",
        "availability": "in-stock",
    }
