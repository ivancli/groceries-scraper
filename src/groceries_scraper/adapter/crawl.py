from scrapy.crawler import CrawlerProcess

from groceries_scraper.adapter.settings import scrapy_settings
from groceries_scraper.adapter.spider import SiteSpider
from groceries_scraper.config import Site
from groceries_scraper.run import Run


def crawl(site: Site, run: Run, limit: int | None = None) -> None:
    """Blocks until the crawl ends; Twisted allows one per process."""
    process = CrawlerProcess(scrapy_settings(site, run, limit))
    process.crawl(SiteSpider, site=site)
    process.start()
