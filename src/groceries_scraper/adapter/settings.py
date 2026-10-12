"""Site `settings:` → Scrapy settings."""

from typing import TYPE_CHECKING, Any

from scrapy.settings import default_settings

from groceries_scraper.config import Site
from groceries_scraper.run import Run

if TYPE_CHECKING:
    from groceries_scraper.adapter.replay import ReplayIndex
    from groceries_scraper.adapter.traffic import RetailerTraffic

# Custom settings carrying the Run and `--limit` to the Record pipeline.
RUN = "GROCERIES_RUN"
RECORD_LIMIT = "GROCERIES_RECORD_LIMIT"
RECORDER = "GROCERIES_RECORDER"
REPLAY = "GROCERIES_REPLAY"


def scrapy_settings(
    site: Site,
    run: Run,
    limit: int | None = None,
    replay: "ReplayIndex | None" = None,
    traffic: "RetailerTraffic | None" = None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "DOWNLOAD_DELAY": site.settings.download_delay,
        "CONCURRENT_REQUESTS_PER_DOMAIN": site.settings.concurrent_requests_per_domain,
        "ROBOTSTXT_OBEY": site.settings.obey_robots,
        "ITEM_PIPELINES": {"groceries_scraper.adapter.pipelines.RecordPipeline": 300},
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": None,
            "groceries_scraper.adapter.middlewares.SharedRobotsTxtMiddleware": 100,
            "groceries_scraper.adapter.middlewares.RefreshStatusMiddleware": 560,
            "groceries_scraper.adapter.middlewares.CaptureMiddleware": 800,
        },
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "INFO",
        RUN: run,
        # The pipeline drops Records still in flight once the spider starts closing.
        RECORD_LIMIT: limit,
    }
    if limit is not None:
        settings["CLOSESPIDER_ITEMCOUNT"] = limit
    if site.settings.user_agent is not None:
        settings["USER_AGENT"] = site.settings.user_agent
    # Replay serves rendered Captures, so it never needs a browser.
    if replay is None and any(page.renders_in_browser for page in site.page_types.values()):
        # Requests without the browser meta key still go through Scrapy's HTTP handler.
        handler = "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler"
        settings["DOWNLOAD_HANDLERS"] = {"http": handler, "https": handler}
        settings["TWISTED_REACTOR"] = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
        # Retried like an HTTP download timeout.
        settings["RETRY_EXCEPTIONS"] = [
            *default_settings.RETRY_EXCEPTIONS,
            "playwright.async_api.TimeoutError",
        ]
    if traffic is not None and replay is None:
        _coordinate(settings, site, traffic)
    if replay is not None:
        settings[REPLAY] = replay
        # After CaptureMiddleware's process_request, so served Captures are timed and recorded.
        settings["DOWNLOADER_MIDDLEWARES"]["groceries_scraper.adapter.replay.ReplayMiddleware"] = (
            900
        )
        # robots.txt was never captured; the source Run already obeyed it.
        settings["ROBOTSTXT_OBEY"] = False
        settings["DOWNLOAD_DELAY"] = 0
    return settings


def _coordinate(settings: dict[str, Any], site: Site, traffic: "RetailerTraffic") -> None:
    from groceries_scraper.adapter.traffic import TRAFFIC, BrowserPermits

    settings[TRAFFIC] = traffic
    browser = any(page.renders_in_browser for page in site.page_types.values())
    handler = "groceries_scraper.adapter.traffic.Coordinated" + (
        "PlaywrightDownloadHandler" if browser else "HTTPDownloadHandler"
    )
    settings["DOWNLOAD_HANDLERS"] = {"http": handler, "https": handler}
    if browser:
        settings["PLAYWRIGHT_ABORT_REQUEST"] = BrowserPermits(traffic)
        # A service worker's requests bypass page routing, and so the permits.
        settings["PLAYWRIGHT_CONTEXTS"] = {"default": {"service_workers": "block"}}
