"""Site `settings:` → Scrapy settings."""

from typing import Any

from groceries_scraper.config import Site
from groceries_scraper.run import Run

# Custom setting carrying the Run to the Record pipeline.
RUN = "GROCERIES_RUN"


def scrapy_settings(site: Site, run: Run, limit: int | None = None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "DOWNLOAD_DELAY": site.settings.download_delay,
        "CONCURRENT_REQUESTS_PER_DOMAIN": site.settings.concurrent_requests_per_domain,
        "ROBOTSTXT_OBEY": site.settings.obey_robots,
        "ITEM_PIPELINES": {"groceries_scraper.adapter.pipelines.RecordPipeline": 300},
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "INFO",
        RUN: run,
    }
    if limit is not None:
        # The pipeline also reads this, to drop Records already in flight at close.
        settings["CLOSESPIDER_ITEMCOUNT"] = limit
    return settings
