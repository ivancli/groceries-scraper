"""One generic spider: every response goes through the engine; Follow Requests go back out."""

import itertools
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any, NoReturn

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response
from twisted.python.failure import Failure

from groceries_scraper.adapter.pipelines import EmittedRecord
from groceries_scraper.config import Site
from groceries_scraper.engine.follow import FollowRequest, rerender
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext
from groceries_scraper.engine.session import (
    Refresh,
    SessionRefresh,
    evaluate_setup,
    setup_request,
)

# Close reason and stats key for Run Health.
SESSION_SETUP_FAILED = "session_setup_failed"


_ERROR_STATUSES = list(range(400, 600))


def _content_type(response: Response) -> str | None:
    value = response.headers.get("Content-Type")
    return value.decode("latin-1") if value else None


class SiteSpider(scrapy.Spider):
    name = "site"  # replaced by the Site's name

    def __init__(self, site: Site, **kwargs: Any) -> None:
        super().__init__(name=site.site, **kwargs)
        self.site = site
        self.env = dict(os.environ)
        self._capture_nos = itertools.count(1)
        self._session: dict[str, Any] = {}
        self._refresh = (
            SessionRefresh(site.session.refresh_on, site.session.max_refresh)
            if site.session
            else None
        )
        # Requests held until Session Setup ends: Start Requests, then refresh retries.
        self._waiting: list[FollowRequest] = []

    async def start(self) -> AsyncIterator[scrapy.Request]:
        starts = [
            FollowRequest("GET", start.url, {}, None, start.page_type, {}, None)
            for start in self.site.start
        ]
        if self.site.session:
            self._waiting = starts
            yield self._setup(0, {})
            return
        for follow in starts:
            yield self._request(follow)

    def _request(self, follow: FollowRequest, resend: bool = False) -> scrapy.Request:
        meta: dict[str, Any] = {}
        if self._refresh is not None:
            meta["handle_httpstatus_list"] = sorted(self._refresh.refresh_on)
        return scrapy.Request(
            follow.url,
            method=follow.method,
            headers=follow.headers,
            body=follow.body,
            callback=self._on_response,
            cb_kwargs={"follow": follow, "generation": self._generation},
            meta=meta,
            dont_filter=resend,  # held or retried: the dupe filter has seen it
        )

    @property
    def _generation(self) -> int:
        return self._refresh.generation if self._refresh else 0

    def _on_response(
        self, response: Response, follow: FollowRequest, generation: int
    ) -> Iterator[EmittedRecord | scrapy.Request]:
        if self._refresh is not None:
            action = self._refresh.on_status(response.status, generation)
            if action is not Refresh.PROCEED:
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
        self, action: Refresh, response: Response, follow: FollowRequest
    ) -> Iterator[scrapy.Request]:
        assert self.site.session is not None
        if action is Refresh.REFRESH:
            self.logger.info("HTTP %d from %s: refreshing the Session", response.status, follow.url)
            self._stats("session/refreshes")
            self._waiting.append(follow)
            yield self._setup(0, {})
        elif action is Refresh.WAIT:
            self._waiting.append(follow)
        elif action is Refresh.RETRY:
            yield self._request(rerender(follow, self._session), resend=True)
        else:
            self.logger.error(
                "HTTP %d from %s: max_refresh (%d) reached; dropping the request",
                response.status,
                follow.url,
                self.site.session.max_refresh,
            )
            self._stats("session/refresh_exhausted")

    # --- Session Setup -------------------------------------------------------

    def _setup(self, index: int, session: dict[str, Any]) -> scrapy.Request:
        assert self.site.session is not None
        step = self.site.session.setup[index]
        try:
            rendered = setup_request(step, PipeContext(session=session, env=self.env))
        except Exception as exc:
            self._setup_failed(index, f"{type(exc).__name__}: {exc}")
        return scrapy.Request(
            rendered.url,
            method=rendered.method,
            headers=rendered.headers,
            body=rendered.body,
            callback=self._on_setup,
            errback=self._on_setup_error,
            cb_kwargs={"index": index, "session": session},
            meta={"handle_httpstatus_list": _ERROR_STATUSES},  # 3xx still redirect
            dont_filter=True,  # Setup re-runs on every refresh
        )

    def _on_setup(
        self, response: Response, index: int, session: dict[str, Any]
    ) -> Iterator[scrapy.Request]:
        assert self.site.session is not None
        result = evaluate_setup(
            self.site.session.setup[index],
            response.status,
            response.body,
            _content_type(response),
            PipeContext(session=session, env=self.env, url=response.url),
        )
        if result.error is not None:
            self._setup_failed(index, result.error)
        session = {**session, **result.session}
        if index + 1 < len(self.site.session.setup):
            yield self._setup(index + 1, session)
            return
        assert self._refresh is not None
        self._session = session
        self._refresh.refreshed()
        waiting, self._waiting = self._waiting, []
        for follow in waiting:
            yield self._request(rerender(follow, session), resend=True)

    def _on_setup_error(self, failure: Failure) -> None:
        request = failure.request  # type: ignore[attr-defined]  # set by Scrapy for errbacks
        self._setup_failed(request.cb_kwargs["index"], failure.getErrorMessage())

    def _setup_failed(self, index: int, error: str) -> NoReturn:
        self.logger.error("Session Setup step %d failed: %s", index, error)
        self._stats(SESSION_SETUP_FAILED)
        raise CloseSpider(SESSION_SETUP_FAILED)

    def _stats(self, key: str) -> None:
        assert self.crawler.stats is not None
        self.crawler.stats.inc_value(key)
