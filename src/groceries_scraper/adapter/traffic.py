"""Retailer request permits for a price Run, taken as each request is actually sent."""

import asyncio
import os
import socket
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from scrapy import Request
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.crawler import Crawler
from scrapy.http import Response
from scrapy_playwright.handler import ScrapyPlaywrightDownloadHandler

from groceries_scraper.adapter.middlewares import BROWSER
from groceries_scraper.config import Site
from groceries_scraper.coordination import Coordinator, CoordinatorUnavailable, Permit, Policy

if TYPE_CHECKING:
    from playwright.async_api import Page
    from playwright.async_api import Request as BrowserRequest

TRAFFIC = "GROCERIES_TRAFFIC"
# Outlasts a request bounded by Scrapy's DOWNLOAD_TIMEOUT, so only a crashed Job's lapses.
LEASE = timedelta(minutes=5)
MAX_POLL = 0.5  # seconds between tries while the retailer's requests in flight are full


@dataclass
class RetailerTraffic:
    """Shared by the download handlers and the browser hook; deep-copied with the settings."""

    dsn: str = field(repr=False)
    retailer: str
    hosts: tuple[str, ...]
    policy: Policy
    holder: str
    _coordinator: Coordinator | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_environment(cls, site: Site) -> "RetailerTraffic | None":
        """Coordinated when the Job is given the coordination Secret."""
        dsn = os.environ.get("SCRAPE_COORDINATOR")
        if not dsn or site.accepts is None:
            return None
        password = os.environ.get("SCRAPE_COORDINATOR_PASSWORD")
        if password:
            from psycopg.conninfo import make_conninfo

            dsn = make_conninfo(dsn, password=password)
        holder = os.environ.get("SCRAPE_JOB_NAME") or f"{socket.gethostname()}:{os.getpid()}"
        return cls(
            dsn, site.accepts.retailer.key, site.accepts.retailer_hosts, site_policy(site), holder
        )

    def covers(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == known or host.endswith("." + known) for known in self.hosts)

    async def acquire(self, *, paced: bool = True, charge: bool = False) -> Permit:
        """Waits for the retailer's pace and concurrency; never sends uncoordinated."""
        while True:
            result = await asyncio.to_thread(
                self._client().acquire_permit,
                self.retailer,
                self.holder,
                self.policy,
                lease=LEASE,
                paced=paced,
                charge=charge,
            )
            if isinstance(result, Permit):
                return result
            if isinstance(result, str):  # refusals are for search executions only
                raise CoordinatorUnavailable(f"a leased permit was refused: {result}")
            delay = MAX_POLL
            if result.until is not None:
                delay = (result.until - datetime.now(UTC)).total_seconds()
            await asyncio.sleep(min(max(delay, 0.01), MAX_POLL))

    async def release(self, permit: Permit) -> None:
        await asyncio.to_thread(self._client().release_permit, permit)

    def _client(self) -> Coordinator:
        if self._coordinator is None:
            self._coordinator = Coordinator(self.dsn)
        return self._coordinator


def site_policy(site: Site) -> Policy:
    return Policy(site.settings.download_delay, site.settings.concurrent_requests_per_domain)


class _Gated:
    """Scrapy-level downloads: robots.txt, Session Setup, each redirect hop and retry."""

    _crawler: Crawler

    async def download_request(self, request: Request) -> Response:
        traffic: RetailerTraffic | None = self._crawler.settings.get(TRAFFIC)
        # A browser request is gated per network request by BrowserPermits instead.
        if traffic is None or request.meta.get(BROWSER) or not traffic.covers(request.url):
            return await super().download_request(request)  # type: ignore[misc,no-any-return]
        permit = await traffic.acquire()
        try:
            return await super().download_request(request)  # type: ignore[misc,no-any-return]
        finally:
            await traffic.release(permit)


class CoordinatedHTTPDownloadHandler(_Gated, HTTP11DownloadHandler):
    pass


class CoordinatedPlaywrightDownloadHandler(_Gated, ScrapyPlaywrightDownloadHandler):  # type: ignore[misc]
    pass


@dataclass
class BrowserPermits:
    """PLAYWRIGHT_ABORT_REQUEST: a permit for every routed request to the retailer.

    Navigations are paced; scripts, fetch/XHR and other resources count only toward requests
    in flight. Playwright doesn't route redirect hops, so those are charged once sent.
    """

    traffic: RetailerTraffic
    _held: dict[Any, Permit] = field(default_factory=dict, repr=False)
    # Retailer redirect hops that finished before their charge was recorded.
    _done: set[Any] = field(default_factory=set, repr=False)
    _pages: "weakref.WeakSet[Page]" = field(default_factory=weakref.WeakSet, repr=False)

    async def __call__(self, request: "BrowserRequest") -> bool:
        if not self.traffic.covers(request.url):
            return False
        page = request.frame.page
        self._watch(page)
        try:
            permit = await self.traffic.acquire(paced=request.is_navigation_request())
        except CoordinatorUnavailable:
            return True  # aborted: an uncoordinated request is never sent
        if page.is_closed():  # its close already released what it held
            await self.traffic.release(permit)
            return True
        self._held[request] = permit
        return False

    def _watch(self, page: "Page") -> None:
        if page in self._pages:
            return
        self._pages.add(page)
        page.on("request", self._charge_redirect)
        page.on("requestfinished", self._finished)
        page.on("requestfailed", self._finished)
        page.on("close", self._closed)

    async def _charge_redirect(self, request: "BrowserRequest") -> None:
        if request.redirected_from is None or not self.traffic.covers(request.url):
            return
        paced = request.is_navigation_request()
        permit = await self.traffic.acquire(paced=paced, charge=True)
        if request in self._done or request.frame.page.is_closed():
            self._done.discard(request)
            await self.traffic.release(permit)
        else:
            self._held[request] = permit

    async def _finished(self, request: "BrowserRequest") -> None:
        if (permit := self._held.pop(request, None)) is not None:
            await self.traffic.release(permit)
        elif request.redirected_from is not None and self.traffic.covers(request.url):
            self._done.add(request)

    async def _closed(self, page: "Page") -> None:
        for request in [r for r in self._held if r.frame.page is page]:
            await self._finished(request)
        self._done = {r for r in self._done if r.frame.page is not page}
