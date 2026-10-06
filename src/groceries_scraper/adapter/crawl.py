import os
import time

from scrapy.crawler import CrawlerProcess

from groceries_scraper.adapter.replay import SourceRun
from groceries_scraper.adapter.settings import RECORDER, scrapy_settings
from groceries_scraper.adapter.spider import SiteSpider
from groceries_scraper.config import Site
from groceries_scraper.run import Run
from groceries_scraper.run.health import assess_health
from groceries_scraper.run.recording import RunRecorder
from groceries_scraper.run.summary import RunOutcome
from groceries_scraper.run.supply import SuppliedStartRequest


def crawl(
    site: Site,
    run: Run,
    limit: int | None = None,
    replay_of: SourceRun | None = None,
    supply: list[SuppliedStartRequest] | None = None,
) -> RunOutcome:
    """Blocks until the crawl ends; Twisted allows one per process."""
    index = replay_of.index if replay_of is not None else None
    settings = scrapy_settings(site, run, limit, index)
    source = replay_of.run if replay_of is not None else None
    # Set by a Kubernetes Job through the Downward API, to trace the Run back to it.
    job = os.environ.get("SCRAPE_JOB_NAME")
    settings[RECORDER] = RunRecorder(run, site, source, job, supply)
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(SiteSpider)
    location = site.locations.get(run.location, {})
    process.crawl(crawler, site=site, location=location, supply=supply)
    started = time.monotonic()
    try:
        process.start()
    finally:
        # The crawler deep-copies its settings: read back the recorder it actually used.
        recorder: RunRecorder = crawler.settings[RECORDER]
        stats = recorder.stats
        stats.duration_seconds = time.monotonic() - started
        if crawler.stats is not None:
            stats.finish_reason = crawler.stats.get_value("finish_reason")
        outcome = RunOutcome(stats, assess_health(site, stats))
        recorder.finish(outcome)
    return outcome
