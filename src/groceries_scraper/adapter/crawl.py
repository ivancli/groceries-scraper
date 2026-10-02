from scrapy.crawler import CrawlerProcess

from groceries_scraper.adapter.settings import RECORDER, scrapy_settings
from groceries_scraper.adapter.spider import SiteSpider
from groceries_scraper.config import Site
from groceries_scraper.run import Run
from groceries_scraper.run.recording import RunRecorder


def crawl(site: Site, run: Run, limit: int | None = None) -> None:
    """Blocks until the crawl ends; Twisted allows one per process."""
    settings = scrapy_settings(site, run, limit)
    settings[RECORDER] = RunRecorder(run, site)
    process = CrawlerProcess(settings)
    process.crawl(SiteSpider, site=site)
    process.start()
