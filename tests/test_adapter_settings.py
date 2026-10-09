from pathlib import Path

import yaml

from groceries_scraper.adapter.replay import ReplayIndex
from groceries_scraper.adapter.settings import RECORD_LIMIT, REPLAY, scrapy_settings
from groceries_scraper.config import Site, parse_site
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


def test_user_agent_overrides_scrapys_only_when_set() -> None:
    assert "USER_AGENT" not in scrapy_settings(_site(), RUN)
    settings = scrapy_settings(_site("{user_agent: Mozilla/5.0}"), RUN)
    assert settings["USER_AGENT"] == "Mozilla/5.0"


def test_a_site_can_opt_out_of_robots_txt() -> None:
    settings = scrapy_settings(_site("{obey_robots: false}"), RUN)

    assert settings["ROBOTSTXT_OBEY"] is False


def test_limit_closes_the_spider_after_n_records() -> None:
    assert "CLOSESPIDER_ITEMCOUNT" not in scrapy_settings(_site(), RUN)
    settings = scrapy_settings(_site(), RUN, limit=3)
    assert settings["CLOSESPIDER_ITEMCOUNT"] == settings[RECORD_LIMIT] == 3


def test_refresh_statuses_are_seen_before_retry_middleware() -> None:
    middlewares = scrapy_settings(_site(), RUN)["DOWNLOADER_MIDDLEWARES"]

    # Responses pass downloader middlewares from the highest order down; Retry is 550.
    assert middlewares["groceries_scraper.adapter.middlewares.RefreshStatusMiddleware"] > 550


def test_replay_serves_captures_without_robots_txt_or_delays() -> None:
    index = ReplayIndex([], frozenset())

    settings = scrapy_settings(_site("{download_delay: 2.0}"), RUN, replay=index)

    assert settings[REPLAY] is index
    middlewares = settings["DOWNLOADER_MIDDLEWARES"]
    replay = middlewares["groceries_scraper.adapter.replay.ReplayMiddleware"]
    assert replay > middlewares["groceries_scraper.adapter.middlewares.CaptureMiddleware"]
    assert settings["ROBOTSTXT_OBEY"] is False  # robots.txt is never captured
    assert settings["DOWNLOAD_DELAY"] == 0
    assert "ReplayMiddleware" not in str(scrapy_settings(_site(), RUN)["DOWNLOADER_MIDDLEWARES"])
