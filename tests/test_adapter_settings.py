from pathlib import Path

import yaml

from groceries_scraper.adapter.settings import RECORD_LIMIT, scrapy_settings
from groceries_scraper.config import Site, parse_site
from groceries_scraper.config.models import Session
from groceries_scraper.run import Run

DEFAULTS = {
    "download_delay": 1.0,
    "concurrent_requests_per_domain": 2,
    "obey_robots": True,
    "record_level": "all",
}
RUN = Run("s", "r1", Path("/runs/s/r1"))


def _site(settings: str = "{}") -> Site:
    return parse_site(
        yaml.safe_load(f"""
site: s
settings: {settings}
start: [{{url: "https://x.example/", page_type: listing}}]
page_types: {{listing: {{}}}}
"""),
        DEFAULTS,
    )


def test_site_settings_map_to_scrapy_settings() -> None:
    settings = scrapy_settings(_site("{download_delay: 0.5}"), RUN)

    assert settings["DOWNLOAD_DELAY"] == 0.5
    assert settings["CONCURRENT_REQUESTS_PER_DOMAIN"] == 2
    assert settings["ROBOTSTXT_OBEY"] is True


def test_a_site_can_opt_out_of_robots_txt() -> None:
    settings = scrapy_settings(_site("{obey_robots: false}"), RUN)

    assert settings["ROBOTSTXT_OBEY"] is False


def test_limit_closes_the_spider_after_n_records() -> None:
    assert "CLOSESPIDER_ITEMCOUNT" not in scrapy_settings(_site(), RUN)
    settings = scrapy_settings(_site(), RUN, limit=3)
    assert settings["CLOSESPIDER_ITEMCOUNT"] == settings[RECORD_LIMIT] == 3


def test_refresh_statuses_refresh_the_session_instead_of_retrying_with_it() -> None:
    site = _site().model_copy(
        update={"session": Session.model_validate({"setup": [], "refresh_on": [419, 429]})}
    )

    codes = scrapy_settings(site, RUN)["RETRY_HTTP_CODES"]

    assert 429 not in codes
    assert 503 in codes
