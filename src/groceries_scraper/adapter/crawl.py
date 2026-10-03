import time

from scrapy.crawler import CrawlerProcess

from groceries_scraper.adapter.settings import RECORDER, STATS, scrapy_settings
from groceries_scraper.adapter.spider import SiteSpider
from groceries_scraper.config import Site
from groceries_scraper.run import Run
from groceries_scraper.run.health import RunHealth, assess_health
from groceries_scraper.run.recording import RunRecorder
from groceries_scraper.run.stats import RunStats


def crawl(site: Site, run: Run, limit: int | None = None) -> tuple[RunStats, RunHealth]:
    """Blocks until the crawl ends; Twisted allows one per process."""
    settings = scrapy_settings(site, run, limit)
    settings[RECORDER] = RunRecorder(run, site)
    settings[STATS] = RunStats.for_site(site)
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(SiteSpider)
    process.crawl(crawler, site=site)
    started = time.monotonic()
    process.start()
    # The crawler deep-copies its settings: read back the objects it actually used.
    recorder: RunRecorder = crawler.settings[RECORDER]
    stats: RunStats = crawler.settings[STATS]
    stats.duration_seconds = time.monotonic() - started
    health = assess_health(site, stats)
    recorder.finish(stats, health)
    return stats, health
