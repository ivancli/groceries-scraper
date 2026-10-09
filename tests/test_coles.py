from datetime import timedelta
from pathlib import Path

from groceries_scraper.config import load_checked_site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.coles.com.au/product/x-160g-1157085"


def _record(pricing: str) -> dict[str, object]:
    site, _ = load_checked_site(ROOT / "sites" / "coles_picks.yaml")
    body = f"""
    <link rel="canonical" href="{URL}">
    <script id="__NEXT_DATA__" type="application/json">
      {{"props": {{"pageProps": {{"product": {{
        "name": "Coconut Yoghurt", "brand": "Cocobella", "size": "160g", "pricing": {pricing}
      }}}}}}}}
    </script>
    """.encode()
    result = evaluate_response(site.page_types["product"], body, "text/html", PipeContext(url=URL))
    assert result.extraction.dropped == []
    return result.extraction.records[0].data


def test_coles_picks_accepts_product_urls_every_30_minutes() -> None:
    site, _ = load_checked_site(ROOT / "sites" / "coles_picks.yaml")

    assert site.schedule is not None and site.schedule.every == timedelta(minutes=30)
    assert site.accepts is not None
    assert site.accepts.matches(URL)
    assert not site.accepts.matches("https://www.coles.com.au/browse/dairy-eggs-fridge")
    assert site.settings.user_agent is not None and "Chrome" in site.settings.user_agent


def test_coles_special_reads_its_was_price_as_the_regular_price() -> None:
    record = _record('{"now": 2, "was": 2.9, "comparable": "$1.25/ 100g"}')

    assert (record["price"], record["regular_price"]) == (2.0, 2.9)
    assert record["unit_price_text"] == "$1.25/ 100g"
    assert (record["name"], record["brand"], record["size"]) == (
        "Coconut Yoghurt",
        "Cocobella",
        "160g",
    )


def test_coles_regular_price_is_null_off_special() -> None:
    assert _record('{"now": 4.95, "was": null}')["regular_price"] is None
    assert _record('{"now": 4.95, "was": 0}')["regular_price"] is None
