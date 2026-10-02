"""One generic spider: every response goes through the engine; Follow Requests go back out."""

import itertools
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any, NoReturn

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response
from twisted.python.failure import Failure

from groceries_scraper.adapter.middlewares import REFRESH_ON
from groceries_scraper.adapter.pipelines import EmittedRecord
from groceries_scraper.config import Site
from groceries_scraper.config.models import Session
from groceries_scraper.engine.follow import FollowRequest
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext
from groceries_scraper.engine.request import RenderedRequest, RequestSource
from groceries_scraper.engine.session import RefreshAction, SessionRefresh, evaluate_setup

# Close reason and stats key for Run Health.
SESSION_SETUP_FAILED = "session_setup_failed"

_ERROR_STATUSES = list(range(400, 600))


def _content_type(response: Response) -> str | None:
    value = response.headers.get("Content-Type")
    return value.decode("latin-1") if value else None


def _scrapy_request(rendered: RenderedRequest, **kwargs: Any) -> scrapy.Request:
    return scrapy.Request(
        rendered.url,
        method=rendered.method,
        headers=rendered.headers,
        body=rendered.body,
        **kwargs,
    )


class SiteSpider(scrapy.Spider):
    name = "site"  # replaced by the Site's name

    def __init__(self, site: Site, **kwargs: Any) -> None:
        super().__init__(name=site.site, **kwargs)
        self.site = site
        self.env = dict(os.environ)
        self._capture_nos = itertools.count(1)
        # No Session: no Setup steps, and a refresh policy that never triggers.
        session = site.session or Session(setup=[])
        self._setup_steps = session.setup
        self._refresh = SessionRefresh(session.refresh_on, session.max_refresh)
        self._session: dict[str, Any] = {}
        # Held until Session Setup ends: Start Requests once, then retries after each refresh.
        self._starts: list[FollowRequest] = []
        self._retries: list[FollowRequest] = []

    async def start(self) -> AsyncIterator[scrapy.Request]:
        starts = [
            FollowRequest(RenderedRequest("GET", start.url), start.page_type, {}, None)
            for start in self.site.start
        ]
        if self._setup_steps:
            self._starts = starts
            yield self._setup(0, {})
            return
        for follow in starts:
            yield self._request(follow)

    def _request(self, follow: FollowRequest, retry: bool = False) -> scrapy.Request:
        refresh_on = sorted(self._refresh.refresh_on)
        return _scrapy_request(
            follow.request,
            callback=self._on_response,
            cb_kwargs={"follow": follow, "generation": self._refresh.generation},
            meta={"handle_httpstatus_list": refresh_on, REFRESH_ON: refresh_on},
            dont_filter=retry,  # the dupe filter saw the original
        )

    def _on_response(
        self, response: Response, follow: FollowRequest, generation: int
    ) -> Iterator[EmittedRecord | scrapy.Request]:
        action = self._refresh.on_status(response.status, generation)
        if action is not RefreshAction.PROCEED:
            yield from self._on_refresh_status(action, response, follow)
            return
        capture_no = next(self._capture_nos)
        result = evaluate_response(
            self.site.page_types[follow.page_type],
            response.body,
            _content_type(response),
            PipeContext(
                variables=follow.variables, session=self._session, env=self.env, url=response.url
            ),
            parent_ref=capture_no,
        )
        for record in result.extraction.records:
            yield EmittedRecord(record, response.url, capture_no)
        for request in result.follow.requests:
            yield self._request(request)

    def _on_refresh_status(
        self, action: RefreshAction, response: Response, follow: FollowRequest
    ) -> Iterator[scrapy.Request]:
        url = follow.request.url
        if action is RefreshAction.REFRESH:
            self.logger.info("HTTP %d from %s: refreshing the Session", response.status, url)
            self._inc_stat("session/refreshes")
            self._retries.append(follow)
            yield self._setup(0, {})
        elif action is RefreshAction.WAIT:
            self._retries.append(follow)
        elif action is RefreshAction.RETRY:
            yield self._request(follow.with_session(self._session), retry=True)
        else:
            self.logger.error(
                "HTTP %d from %s: max_refresh (%d) reached; dropping the request",
                response.status,
                url,
                self._refresh.max_refresh,
            )
            self._inc_stat("session/refresh_exhausted")

    # --- Session Setup -------------------------------------------------------

    def _setup(self, index: int, session: dict[str, Any]) -> scrapy.Request:
        # Each step sees only the Session Variables extracted before it.
        ctx = PipeContext(session=session, env=self.env)
        try:
            rendered = RequestSource(self._setup_steps[index].request, None, ctx).render()
        except Exception as exc:
            self._setup_failed(index, f"{type(exc).__name__}: {exc}")
        return _scrapy_request(
            rendered,
            callback=self._on_setup,
            errback=self._on_setup_error,
            cb_kwargs={"index": index, "session": session},
            meta={"handle_httpstatus_list": _ERROR_STATUSES},  # 3xx still redirect
            dont_filter=True,  # Setup re-runs on every refresh
        )

    def _on_setup(
        self, response: Response, index: int, session: dict[str, Any]
    ) -> Iterator[scrapy.Request]:
        result = evaluate_setup(
            self._setup_steps[index],
            response.status,
            response.body,
            _content_type(response),
            PipeContext(session=session, env=self.env, url=response.url),
        )
        if result.error is not None:
            self._setup_failed(index, result.error)
        session = {**session, **result.session}
        if index + 1 < len(self._setup_steps):
            yield self._setup(index + 1, session)
            return
        self._session = session
        self._refresh.refreshed()
        starts, self._starts = self._starts, []
        retries, self._retries = self._retries, []
        for follow in starts:
            yield self._request(follow)
        for follow in retries:
            yield self._request(follow.with_session(session), retry=True)

    def _on_setup_error(self, failure: Failure) -> None:
        request = failure.request  # type: ignore[attr-defined]  # set by Scrapy for errbacks
        self._setup_failed(request.cb_kwargs["index"], failure.getErrorMessage())

    def _setup_failed(self, index: int, error: str) -> NoReturn:
        self.logger.error("Session Setup step %d failed: %s", index, error)
        self._inc_stat(SESSION_SETUP_FAILED)
        raise CloseSpider(SESSION_SETUP_FAILED)

    def _inc_stat(self, key: str) -> None:
        assert self.crawler.stats is not None
        self.crawler.stats.inc_value(key)
