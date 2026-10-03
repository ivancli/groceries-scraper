import time

from scrapy.crawler import CrawlerProcess

from groceries_scraper.adapter.settings import RECORDER, scrapy_settings
from groceries_scraper.adapter.spider import SiteSpider
from groceries_scraper.config import Site
from groceries_scraper.run import Run
from groceries_scraper.run.health import assess_health
from groceries_scraper.run.recording import RunRecorder
from groceries_scraper.run.summary import RunOutcome


def crawl(site: Site, run: Run, limit: int | None = None) -> RunOutcome:
    """Blocks until the crawl ends; Twisted allows one per process."""
    settings = scrapy_settings(site, run, limit)
    settings[RECORDER] = RunRecorder(run, site)
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(SiteSpider)
    process.crawl(crawler, site=site)
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
