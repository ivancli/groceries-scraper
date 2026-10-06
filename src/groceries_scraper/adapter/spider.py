"""One generic spider: every response goes through the engine; Follow Requests go back out."""

import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import scrapy
from scrapy.exceptions import CloseSpider, IgnoreRequest
from scrapy.http import Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from groceries_scraper.adapter.middlewares import (
    BROWSER,
    CAPTURE,
    PAGE_TYPE,
    PARENT_CAPTURE,
    REFRESH_ON,
    SESSION_NO,
    SOURCE_FETCHED_AT,
    VARIABLES,
)
from groceries_scraper.adapter.pipelines import EmittedRecord
from groceries_scraper.adapter.settings import RECORDER
from groceries_scraper.config import Site
from groceries_scraper.config.models import Session
from groceries_scraper.engine.follow import FollowRequest
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext
from groceries_scraper.engine.request import RenderedRequest, RequestSource
from groceries_scraper.engine.session import RefreshAction, SessionRefresh, evaluate_setup
from groceries_scraper.run.health import SESSION_SETUP_FAILED
from groceries_scraper.run.keys import RecordKeys
from groceries_scraper.run.recording import Capture, RunRecorder
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.supply import SuppliedStartRequest, SupplyOutcomes

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


@dataclass(eq=False)
class _Session:
    number: int  # 1-based; also names its cookie jar
    refresh: SessionRefresh
    ready: bool  # its first Session Setup has ended
    variables: dict[str, Any] = field(default_factory=dict)
    # Held until its Session Setup ends: Start Requests once, then retries after each refresh.
    starts: list[FollowRequest] = field(default_factory=list)
    retries: list[FollowRequest] = field(default_factory=list)

    @property
    def lost(self) -> bool:
        return self.refresh.lost


class SiteSpider(scrapy.Spider):
    name = "site"  # replaced by the Site's name

    def __init__(
        self,
        site: Site,
        location: Mapping[str, Any] | None = None,
        supply: list[SuppliedStartRequest] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=site.site, **kwargs)
        self.site = site
        self._starts = self._start_requests(site, supply)
        self.env = dict(os.environ)
        self._location = location or {}
        # No Session: no Setup steps, and a refresh policy that never triggers.
        session = site.session or Session(setup=[])
        self._setup_steps = session.setup
        self._sessions = [
            _Session(
                number,
                SessionRefresh(session.refresh_on, session.max_refresh),
                ready=not session.setup,
            )
            for number in range(1, session.pool + 1)
        ]
        self._keys = RecordKeys(site.records)

    @staticmethod
    def _start_requests(
        site: Site, supply: list[SuppliedStartRequest] | None
    ) -> list[FollowRequest]:
        if supply is None:
            return [
                FollowRequest(RenderedRequest("GET", start.url), start.page_type, {}, None)
                for start in site.start
            ]
        assert site.accepts is not None  # checked before the Run starts
        page_type = site.accepts.page_type
        return [
            FollowRequest(RenderedRequest("GET", s.url), page_type, {}, None, s.ref) for s in supply
        ]

    async def start(self) -> AsyncIterator[scrapy.Request]:
        # Round-robin in config order, so Replay assigns Sessions identically.
        for i, follow in enumerate(self._starts):
            if request := self._assign(follow, self._sessions[i % len(self._sessions)]):
                yield request
        for session in self._sessions:
            if not session.ready:
                for request in self._setup(session, 0, {}):
                    yield request

    def _assign(self, start: FollowRequest, session: _Session) -> scrapy.Request | None:
        """A Start Request goes out now, or once the Session's Setup ends."""
        if session.ready:
            return self._request(start, session)
        session.starts.append(start)
        return None

    def _request(
        self, follow: FollowRequest, session: _Session, retry: bool = False
    ) -> scrapy.Request:
        refresh_on = sorted(session.refresh.refresh_on)
        source = follow.source
        variables = {
            **follow.variables,
            **(dict(source.bindings) if source is not None else {}),
            "session": dict(source.ctx.session if source is not None else session.variables),
        }
        return _scrapy_request(
            follow.request,
            callback=self._on_response,
            errback=self._on_error,
            cb_kwargs={
                "follow": follow,
                "session": session,
                "generation": session.refresh.generation,
            },
            meta={
                "handle_httpstatus_list": refresh_on,
                "cookiejar": session.number,
                REFRESH_ON: refresh_on,
                PAGE_TYPE: follow.page_type,
                VARIABLES: variables,
                PARENT_CAPTURE: follow.parent_ref,
                **self._render_meta(follow.page_type),
            },
            # The dupe filter saw the original; two refs may supply the same URL.
            dont_filter=retry or follow.supplied_ref is not None,
        )

    def _render_meta(self, page_type: str) -> dict[str, Any]:
        if not self.site.page_types[page_type].renders_in_browser:
            return {}
        # Network idle: scripts that fetch content after `load` have finished rendering it.
        return {BROWSER: True, "playwright_page_goto_kwargs": {"wait_until": "networkidle"}}

    def _on_response(
        self, response: Response, follow: FollowRequest, session: _Session, generation: int
    ) -> Iterator[EmittedRecord | scrapy.Request]:
        action = session.refresh.on_status(response.status, generation)
        if action is not RefreshAction.PROCEED:
            yield from self._on_refresh_status(action, response, follow, session)
            return
        capture: Capture = response.meta[CAPTURE]
        capture_no = capture.capture_no
        self._stats.add_request_ok()
        self._stats.add_page(follow.page_type)
        try:
            result = evaluate_response(
                self.site.page_types[follow.page_type],
                response.body,
                _content_type(response),
                PipeContext(
                    variables=follow.variables,
                    session=session.variables,
                    location=self._location,
                    env=self.env,
                    url=response.url,
                ),
                parent_ref=capture_no,
            )
        except ValueError as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._recorder.record(capture, {"error": error})
            self.logger.error("Extraction failed for %s: %s", response.url, exc)
            self._outcome(follow, lambda outcomes, ref: outcomes.failed(ref, error))
            return
        extraction = self._keys.filter(result.extraction, capture_no)
        self._recorder.record(
            capture,
            {
                "loop": [asdict(step) for step in result.loop] if result.loop is not None else None,
                "fields": [asdict(entry) for entry in extraction.trace],
                "dropped": [asdict(entry) for entry in extraction.dropped],
                "follow": [asdict(entry) for entry in result.follow.trace],
            },
        )
        for dropped in extraction.dropped:
            self._stats.add_dropped(dropped)
        ref = follow.supplied_ref
        # A Replay keeps the source Run's time: that's when the product was seen.
        scraped_at = (
            response.meta.get(SOURCE_FETCHED_AT)
            or capture.meta["response"]["timing"]["finished_at"]
            if ref is not None
            else None
        )
        for record in extraction.records:
            self._stats.add_extracted()
            yield EmittedRecord(record, response.url, capture_no, ref, scraped_at)
        if not extraction.records:
            error = "; ".join(d.reason for d in extraction.dropped) or "no Record extracted"
            self._outcome(follow, lambda outcomes, ref: outcomes.failed(ref, error))
        for request in result.follow.requests:
            yield self._request(request, session)

    def _on_refresh_status(
        self, action: RefreshAction, response: Response, follow: FollowRequest, session: _Session
    ) -> Iterator[scrapy.Request]:
        url = follow.request.url
        capture: Capture = response.meta[CAPTURE]
        follow = replace(follow, parent_ref=capture.capture_no)
        if action is RefreshAction.REFRESH:
            self.logger.info("HTTP %d from %s: refreshing the Session", response.status, url)
            self._inc_stat("session/refreshes")
            session.retries.append(follow)
            yield from self._setup(session, 0, {}, parent=capture.capture_no)
        elif action is RefreshAction.WAIT:
            session.retries.append(follow)
        elif action is RefreshAction.RETRY:
            yield self._request(follow.with_session(session.variables), session, retry=True)
        elif action is RefreshAction.LOST:
            self._stats.add_request_failed("Session lost")
            self._outcome(follow, lambda outcomes, ref: outcomes.failed(ref, "Session lost"))
        else:
            self.logger.error(
                "HTTP %d from %s: max_refresh (%d) reached; dropping the request",
                response.status,
                url,
                session.refresh.max_refresh,
            )
            self._inc_stat("session/refresh_exhausted")
            self._stats.add_request_failed(f"HTTP {response.status}")
            status = response.status
            self._outcome(follow, lambda outcomes, ref: outcomes.http_error(ref, status))
            yield from self._lose(session, "max_refresh reached")

    def _on_error(self, failure: Failure) -> None:
        """A page request's final failure, after Scrapy's retries."""
        request = failure.request  # type: ignore[attr-defined]  # set by Scrapy for errbacks
        follow: FollowRequest = request.cb_kwargs["follow"]
        error = failure.value
        if isinstance(error, HttpError):
            status = error.response.status
            self.logger.info("HTTP %d from %s: not handled", status, request.url)
            self._stats.add_request_failed(f"HTTP {status}")
            self._outcome(follow, lambda outcomes, ref: outcomes.http_error(ref, status))
        elif isinstance(error, IgnoreRequest):  # e.g. robots.txt: never sent
            self._outcome(follow, lambda outcomes, ref: outcomes.skipped(ref))
        else:
            message = failure.getErrorMessage()
            self.logger.error("Request to %s failed: %s", request.url, message)
            self._stats.add_request_failed(type(error).__name__)
            reason = f"{type(error).__name__}: {message}"
            self._outcome(follow, lambda outcomes, ref: outcomes.failed(ref, reason))

    def _outcome(
        self, follow: FollowRequest, report: Callable[[SupplyOutcomes, str], None]
    ) -> None:
        outcomes = self._recorder.outcomes
        if outcomes is not None and follow.supplied_ref is not None:
            report(outcomes, follow.supplied_ref)

    # --- Session Setup -------------------------------------------------------

    def _setup(
        self,
        session: _Session,
        index: int,
        variables: dict[str, Any],
        parent: int | None = None,
    ) -> Iterator[scrapy.Request]:
        # Each step sees only the Session Variables extracted before it.
        ctx = PipeContext(session=variables, location=self._location, env=self.env)
        try:
            rendered = RequestSource(self._setup_steps[index].request, None, ctx).render()
        except Exception as exc:
            yield from self._setup_failed(session, index, f"{type(exc).__name__}: {exc}")
            return
        meta = {
            "handle_httpstatus_list": _ERROR_STATUSES,  # 3xx still redirect
            "cookiejar": session.number,
            PAGE_TYPE: "session_setup",
            PARENT_CAPTURE: parent,
            VARIABLES: {"session": dict(variables)},
        }
        if len(self._sessions) > 1:
            meta[SESSION_NO] = session.number
        yield _scrapy_request(
            rendered,
            callback=self._on_setup,
            errback=self._on_setup_error,
            cb_kwargs={"session": session, "index": index, "variables": variables},
            meta=meta,
            dont_filter=True,  # Setup re-runs on every refresh, and once per Session
        )

    def _on_setup(
        self, response: Response, session: _Session, index: int, variables: dict[str, Any]
    ) -> Iterator[scrapy.Request]:
        result = evaluate_setup(
            self._setup_steps[index],
            response.status,
            response.body,
            _content_type(response),
            PipeContext(session=variables, location=self._location, env=self.env, url=response.url),
        )
        capture: Capture = response.meta[CAPTURE]
        self._recorder.record(
            capture, {"fields": [asdict(entry) for entry in result.trace], "error": result.error}
        )
        if result.error is not None:
            yield from self._setup_failed(session, index, result.error)
            return
        variables = {**variables, **result.session}
        if index + 1 < len(self._setup_steps):
            yield from self._setup(session, index + 1, variables, parent=capture.capture_no)
            return
        session.variables = variables
        session.refresh.refreshed()
        session.ready = True
        starts, session.starts = session.starts, []
        retries, session.retries = session.retries, []
        for follow in starts:
            yield self._request(follow, session)
        for follow in retries:
            yield self._request(follow.with_session(variables), session, retry=True)

    def _on_setup_error(self, failure: Failure) -> list[scrapy.Request]:
        request = failure.request  # type: ignore[attr-defined]  # set by Scrapy for errbacks
        session, index = request.cb_kwargs["session"], request.cb_kwargs["index"]
        return list(self._setup_failed(session, index, failure.getErrorMessage()))

    def _setup_failed(self, session: _Session, index: int, error: str) -> Iterator[scrapy.Request]:
        self.logger.error("Session Setup step %d failed: %s", index, error)
        yield from self._lose(session, "Session Setup failed")

    def _lose(self, session: _Session, reason: str) -> Iterator[scrapy.Request]:
        """Hands its unstarted Start Requests to the remaining Sessions; none left ends the Run."""
        if session.lost:
            return
        session.refresh.lose()
        self.logger.error("Session %d lost: %s", session.number, reason)
        self._inc_stat("session/lost")
        self._stats.add_session_lost()
        # Follow-on requests keep their Session, so its pending retries go with it.
        for follow in session.retries:
            self._stats.add_request_failed("Session lost")
            self._outcome(follow, lambda outcomes, ref: outcomes.failed(ref, "Session lost"))
        session.retries = []
        remaining = [other for other in self._sessions if not other.lost]
        if not remaining:
            raise CloseSpider(SESSION_SETUP_FAILED)
        starts, session.starts = session.starts, []
        for i, follow in enumerate(starts):
            if request := self._assign(follow, remaining[i % len(remaining)]):
                yield request

    def _inc_stat(self, key: str) -> None:
        assert self.crawler.stats is not None
        self.crawler.stats.inc_value(key)

    @property
    def _recorder(self) -> RunRecorder:
        recorder: RunRecorder = self.crawler.settings[RECORDER]
        return recorder

    @property
    def _stats(self) -> RunStats:
        return self._recorder.stats
