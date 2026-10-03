from typing import Any

from groceries_scraper.config import parse_site
from groceries_scraper.config.models import Site
from groceries_scraper.engine.extract import DroppedRecord, Record
from groceries_scraper.run.health import assess_health
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.summary import format_summary

DEFAULTS = {
    "download_delay": 0,
    "concurrent_requests_per_domain": 1,
    "obey_robots": True,
    "record_level": "all",
}


def _site(health: dict[str, Any] | None = None, records: list[str] | None = None) -> Site:
    record_types = records or ["product"]
    return parse_site(
        {
            "site": "s",
            "records": {name: {} for name in record_types},
            "health": health or {},
            "start": [{"url": "https://shop.example/", "page_type": "listing"}],
            "page_types": {
                "listing": {
                    "follow": [
                        {"select": {"css": "a::attr(href)"}, "page_type": name}
                        for name in record_types
                    ]
                },
                **{
                    name: {
                        "record": name,
                        "fields": {
                            "sku": {"css": ".sku::text"},
                            "price": {"css": ".price::text"},
                        },
                    }
                    for name in record_types
                },
            },
        },
        DEFAULTS,
    )


def _stats(site: Site, records: int = 10, price: float | None = 1.0) -> RunStats:
    stats = RunStats.for_site(site)
    for i in range(records):
        stats.add_record(Record("product", {"sku": f"p{i}", "price": price}))
    for _ in range(records):
        stats.add_response(200)
    return stats


def test_a_healthy_run_is_ok_with_exit_code_0() -> None:
    site = _site({"min_records": {"product": 10}, "max_null_ratio": {"price": 0}})

    health = assess_health(site, _stats(site))

    assert (health.status, health.breaches, health.exit_code) == ("ok", [], 0)


def test_min_records_breach_degrades_the_run() -> None:
    site = _site({"min_records": {"product": 11}})

    health = assess_health(site, _stats(site))

    assert (health.status, health.exit_code) == ("degraded", 1)
    assert health.breaches == ["min_records.product: 10 < 11"]


def test_max_dropped_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_dropped_ratio": 0.05})
    stats = _stats(site, records=9)
    stats.add_dropped(DroppedRecord(0, "required Field `sku` is missing", ("x",)))

    health = assess_health(site, stats)

    assert health.status == "degraded"
    assert health.breaches == ["max_dropped_ratio: 0.100 > 0.050"]


def test_max_dropped_ratio_at_the_threshold_is_ok() -> None:
    site = _site({"max_dropped_ratio": 0.1})
    stats = _stats(site, records=9)
    stats.add_dropped(DroppedRecord(0, "reason", ("x",)))

    assert assess_health(site, stats).status == "ok"


def test_max_null_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_null_ratio": {"price": 0.02}})
    stats = _stats(site, records=3)
    stats.add_record(Record("product", {"sku": "x", "price": None}))

    health = assess_health(site, stats)

    assert health.status == "degraded"
    assert health.breaches == ["max_null_ratio.price: 0.250 > 0.020"]


def test_max_http_error_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_http_error_ratio": 0.1})
    stats = _stats(site, records=8)
    stats.add_response(404)
    stats.add_response(503)

    health = assess_health(site, stats)

    assert health.status == "degraded"
    assert health.breaches == ["max_http_error_ratio: 0.200 > 0.100"]


def test_every_breach_is_reported() -> None:
    site = _site({"min_records": {"product": 20}, "max_null_ratio": {"price": 0}})

    health = assess_health(site, _stats(site, price=None))

    assert health.breaches == [
        "min_records.product: 10 < 20",
        "max_null_ratio.price: 1.000 > 0.000",
    ]


def test_session_setup_failure_fails_the_run_with_exit_code_2() -> None:
    site = _site()
    stats = _stats(site)
    stats.session_setup_failed = True

    health = assess_health(site, stats)

    assert (health.status, health.exit_code) == ("failed", 2)
    assert health.breaches == ["Session Setup failed"]


def test_zero_records_for_a_declared_record_type_fails_the_run() -> None:
    site = _site({"min_records": {"promotion": 1}}, records=["product", "promotion"])

    health = assess_health(site, _stats(site))

    assert health.status == "failed"
    assert health.breaches == [
        "no Records of Record Type `promotion`",
        "min_records.promotion: 0 < 1",
    ]


def test_stats_count_pages_records_drops_nulls_and_statuses() -> None:
    site = _site(records=["product", "promotion"])
    stats = RunStats.for_site(site)
    stats.add_page("listing")
    stats.add_page("product")
    stats.add_page("product")
    stats.add_record(Record("product", {"sku": "a", "price": None}))
    stats.add_record(Record("product", {"sku": "b", "price": 2.0}))
    stats.add_dropped(DroppedRecord(0, "...", ("required Field `sku` is missing",)))
    stats.add_dropped(
        DroppedRecord(
            1, "...", ("required Field `sku` is missing", "required Field `price` is invalid")
        )
    )
    stats.add_response(200)
    stats.add_response(200)
    stats.add_response(404)
    stats.duration_seconds = 1.5

    assert stats.to_json() == {
        "duration_seconds": 1.5,
        "pages": {"listing": 1, "product": 2},
        "records": {"product": 2, "promotion": 0},
        "dropped": {
            "total": 2,
            "by_reason": {
                "required Field `sku` is missing": 2,
                "required Field `price` is invalid": 1,
            },
        },
        "null_ratio": {"price": 0.5, "sku": 0.0},
        "http_status": {"200": 2, "404": 1},
    }


def test_summary_is_a_few_lines_ending_in_health_and_breaches() -> None:
    site = _site({"min_records": {"product": 20}})
    stats = _stats(site, records=2)
    stats.add_page("product")
    stats.add_response(404)
    stats.add_dropped(DroppedRecord(0, "...", ("required Field `sku` is missing",)))
    stats.duration_seconds = 1.234

    summary = format_summary(stats, assess_health(site, stats))

    assert summary.splitlines() == [
        "records: product 2",
        "pages: product 1",
        "dropped: 1 (required Field `sku` is missing: 1)",
        "http: 200: 2, 404: 1",
        "duration: 1.2s",
        "health: degraded",
        "  min_records.product: 2 < 20",
    ]
