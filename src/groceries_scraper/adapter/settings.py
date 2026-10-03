"""Site `settings:` → Scrapy settings."""

from typing import TYPE_CHECKING, Any

from groceries_scraper.config import Site
from groceries_scraper.run import Run

if TYPE_CHECKING:
    from groceries_scraper.adapter.replay import ReplayIndex

# Custom settings carrying the Run and `--limit` to the Record pipeline.
RUN = "GROCERIES_RUN"
RECORD_LIMIT = "GROCERIES_RECORD_LIMIT"
RECORDER = "GROCERIES_RECORDER"
REPLAY = "GROCERIES_REPLAY"


def scrapy_settings(
    site: Site, run: Run, limit: int | None = None, replay: "ReplayIndex | None" = None
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "DOWNLOAD_DELAY": site.settings.download_delay,
        "CONCURRENT_REQUESTS_PER_DOMAIN": site.settings.concurrent_requests_per_domain,
        "ROBOTSTXT_OBEY": site.settings.obey_robots,
        "ITEM_PIPELINES": {"groceries_scraper.adapter.pipelines.RecordPipeline": 300},
        "DOWNLOADER_MIDDLEWARES": {
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
