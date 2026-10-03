from typing import Any

from groceries_scraper.config import parse_site
from groceries_scraper.config.models import Site
from groceries_scraper.engine.extract import DroppedRecord, Record
from groceries_scraper.run.health import (
    FINISHED,
    LIMIT_REACHED,
    SESSION_SETUP_FAILED,
    Breach,
    assess_health,
)
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.summary import RunOutcome

DEFAULTS = {
    "download_delay": 0,
    "concurrent_requests_per_domain": 1,
    "obey_robots": True,
    "record_level": "all",
}
PRODUCT_FIELDS = {
    "sku": {"css": ".sku::text"},
    "price": {"css": ".price::text"},
    "brand": {"type": "object", "fields": {"name": {"css": ".brand::text"}}},
    "variants": {
        "type": "array",
        "each": {"css": ".variant"},
        "fields": {"size": {"css": ".size::text"}},
    },
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
                        "fields": PRODUCT_FIELDS
                        if name == "product"
                        else {"price": {"css": ".price::text"}},
                    }
                    for name in record_types
                },
            },
        },
        DEFAULTS,
    )


def _stats(site: Site, records: int = 10, price: float | None = 1.0) -> RunStats:
    stats = RunStats.for_site(site)
    stats.finish_reason = FINISHED
    for i in range(records):
        stats.add_extracted()
        stats.add_record(Record("product", {"sku": f"p{i}", "price": price}))
        stats.add_request_ok()
    return stats


def _breaches(site: Site, stats: RunStats) -> list[str]:
    return [str(breach) for breach in assess_health(site, stats).breaches]


def test_a_healthy_run_is_ok_with_exit_code_0() -> None:
    site = _site({"min_records": {"product": 10}, "max_null_ratio": {"price": 0}})

    health = assess_health(site, _stats(site))

    assert (health.level, health.breaches, health.exit_code) == ("ok", [], 0)


def test_min_records_breach_degrades_the_run() -> None:
    site = _site({"min_records": {"product": 11}})

    health = assess_health(site, _stats(site))

    assert (health.level, health.exit_code) == ("degraded", 1)
    assert health.breaches == [Breach("min_records.product", "10 < 11")]


def test_max_dropped_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_dropped_ratio": 0.05})
    stats = _stats(site, records=9)
    stats.add_dropped(DroppedRecord(0, "required Field `sku` is missing", ("x",)))

    assert assess_health(site, stats).level == "degraded"
    assert _breaches(site, stats) == ["max_dropped_ratio: 0.100 > 0.050"]


def test_max_dropped_ratio_at_the_threshold_is_ok() -> None:
    site = _site({"max_dropped_ratio": 0.1})
    stats = _stats(site, records=9)
    stats.add_dropped(DroppedRecord(0, "reason", ("x",)))

    assert assess_health(site, stats).level == "ok"


def test_dropped_ratio_counts_records_cut_by_the_limit() -> None:
    site = _site({"max_dropped_ratio": 0.1})
    stats = _stats(site, records=1)
    stats.finish_reason = LIMIT_REACHED
    for _ in range(8):
        stats.add_extracted()  # extracted but never written
    stats.add_dropped(DroppedRecord(0, "reason", ("x",)))

    assert stats.dropped_ratio() == 0.1
    assert assess_health(site, stats).level == "ok"


def test_max_null_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_null_ratio": {"price": 0.02}})
    stats = _stats(site, records=3)
    stats.add_record(Record("product", {"sku": "x", "price": None}))

    assert assess_health(site, stats).level == "degraded"
    assert _breaches(site, stats) == ["max_null_ratio.price: 0.250 > 0.020 in `product`"]


def test_max_null_ratio_is_checked_per_record_type() -> None:
    site = _site({"max_null_ratio": {"price": 0.5}}, records=["product", "promotion"])
    stats = _stats(site, records=2, price=None)
    for _ in range(3):
        stats.add_record(Record("promotion", {"price": 1.0}))

    assert _breaches(site, stats) == ["max_null_ratio.price: 1.000 > 0.500 in `product`"]


def test_null_ratio_covers_nested_fields_of_present_objects_and_list_elements() -> None:
    site = _site()
    stats = RunStats.for_site(site)
    stats.add_record(
        Record(
            "product",
            {
                "sku": "a",
                "price": None,
                "brand": {"name": None},
                "variants": [{"size": "1L"}, {"size": None}],
            },
        )
    )
    stats.add_record(Record("product", {"sku": "b", "price": 1.0, "brand": None}))

    assert stats.null_ratio() == {
        "product": {
            "brand": 0.5,
            "brand.name": 1.0,
            "price": 0.5,
            "sku": 0.0,
            "variants": 0.0,
            "variants[].size": 0.5,
        }
    }


def test_max_http_error_ratio_breach_degrades_the_run() -> None:
    site = _site({"max_http_error_ratio": 0.1})
    stats = _stats(site, records=8)
    stats.add_request_failed("HTTP 404")
    stats.add_request_failed("TimeoutError")

    assert assess_health(site, stats).level == "degraded"
    assert _breaches(site, stats) == ["max_http_error_ratio: 0.200 > 0.100"]


def test_http_error_ratio_ignores_statuses_of_retried_and_setup_captures() -> None:
    site = _site({"max_http_error_ratio": 0})
    stats = _stats(site, records=1)
    stats.add_response(503)  # retried, then the request succeeded

    assert assess_health(site, stats).level == "ok"


def test_every_breach_is_reported() -> None:
    site = _site({"min_records": {"product": 20}, "max_null_ratio": {"price": 0}})

    assert _breaches(site, _stats(site, price=None)) == [
        "min_records.product: 10 < 20",
        "max_null_ratio.price: 1.000 > 0.000 in `product`",
    ]


def test_session_setup_failure_fails_the_run_with_exit_code_2() -> None:
    site = _site()
    stats = _stats(site)
    stats.finish_reason = SESSION_SETUP_FAILED

    health = assess_health(site, stats)

    assert (health.level, health.exit_code) == ("failed", 2)
    assert _breaches(site, stats) == ["finish_reason: Session Setup failed"]


def test_zero_records_for_a_declared_record_type_fails_the_run() -> None:
    site = _site({"min_records": {"promotion": 1}}, records=["product", "promotion"])

    assert assess_health(site, _stats(site)).level == "failed"
    assert _breaches(site, _stats(site)) == [
        "records.promotion: no Records",
        "min_records.promotion: 0 < 1",
    ]


def test_a_run_closed_early_fails() -> None:
    site = _site()
    stats = _stats(site)
    stats.finish_reason = "shutdown"

    assert assess_health(site, stats).level == "failed"
    assert _breaches(site, stats) == ["finish_reason: closed early (shutdown)"]


def test_a_run_without_a_finish_reason_fails() -> None:
    site = _site()
    stats = _stats(site)
    stats.finish_reason = None

    assert _breaches(site, stats) == ["finish_reason: closed early (unknown)"]


def test_reaching_the_limit_skips_record_count_checks() -> None:
    site = _site({"min_records": {"product": 5}}, records=["product", "promotion"])
    stats = _stats(site, records=1)
    stats.finish_reason = LIMIT_REACHED

    assert assess_health(site, stats).level == "ok"


def test_stats_count_pages_records_drops_requests_and_statuses() -> None:
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
    stats.add_request_ok()
    stats.add_request_failed("HTTP 404")
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
        "null_ratio": {"product": {"price": 0.5, "sku": 0.0}},
        "requests": {"ok": 1, "failed": {"HTTP 404": 1}, "missing": 0},
        "http_status": {"200": 2, "404": 1},
    }


def test_outcome_saves_stats_and_structured_health() -> None:
    site = _site({"min_records": {"product": 20}})
    stats = _stats(site, records=2)

    outcome = RunOutcome(stats, assess_health(site, stats))

    assert outcome.to_json() == {
        "stats": stats.to_json(),
        "health": {
            "level": "degraded",
            "breaches": [{"check": "min_records.product", "detail": "2 < 20"}],
        },
    }


def test_summary_is_a_few_lines_ending_in_health_and_breaches() -> None:
    site = _site({"min_records": {"product": 20}})
    stats = _stats(site, records=2)
    stats.add_page("product")
    stats.add_request_failed("HTTP 404")
    stats.add_response(200)
    stats.add_response(404)
    stats.add_dropped(DroppedRecord(0, "...", ("required Field `sku` is missing",)))
    stats.duration_seconds = 1.234

    summary = RunOutcome(stats, assess_health(site, stats)).summary()

    assert summary.splitlines() == [
        "records: product 2",
        "pages: product 1",
        "dropped: 1 (required Field `sku` is missing: 1)",
        "requests: ok 2, failed 1 (HTTP 404: 1)",
        "http: 200: 1, 404: 1",
        "duration: 1.2s",
        "health: degraded",
        "  min_records.product: 2 < 20",
    ]
