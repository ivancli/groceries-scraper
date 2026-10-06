"""Capture HTTP exchanges before redirects/retries; keep retries out of Session refresh."""

import base64
import itertools
import time
from datetime import UTC, datetime
from typing import Any, Self

from scrapy import Request
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.robotstxt import RobotsTxtMiddleware
from scrapy.http import Response
from scrapy.http.headers import Headers
from scrapy.robotstxt import RobotParser
from scrapy.utils.httpobj import urlparse_cached
from twisted.internet.defer import Deferred

from groceries_scraper.adapter.fingerprint import request_fingerprint
from groceries_scraper.adapter.settings import RECORDER
from groceries_scraper.run.recording import Capture, RunRecorder

# Request meta: the statuses the spider answers with a Session refresh (page requests only).
REFRESH_ON = "groceries_refresh_on"
PAGE_TYPE = "groceries_page_type"
VARIABLES = "groceries_variables"
PARENT_CAPTURE = "groceries_parent_capture"
CAPTURE = "groceries_capture"
SESSION_NO = "groceries_session_no"  # Session Setup requests, when the pool has several
SOURCE_FETCHED_AT = "groceries_source_fetched_at"  # Replay: when the source Run fetched it
# scrapy-playwright's own key: its download handler renders requests carrying it.
BROWSER = "playwright"
_STARTED = "groceries_capture_started"


def _headers(headers: Headers) -> dict[str, list[str]]:
    return {
        name.decode("latin-1"): [value.decode("latin-1") for value in headers.getlist(name)]
        for name in headers
    }


def request_metadata(request: Request, ignore_params: frozenset[str]) -> dict[str, Any]:
    """A page request's unredacted Capture metadata, before any response."""
    return {
        "page_type": request.meta[PAGE_TYPE],
        **({"render": "browser"} if request.meta.get(BROWSER) else {}),
        "parent_capture_no": request.meta.get(PARENT_CAPTURE),
        **({"session_no": request.meta[SESSION_NO]} if SESSION_NO in request.meta else {}),
        "variables": request.meta.get(VARIABLES, {}),
        "request": {
            "method": request.method,
            "fingerprint": request_fingerprint(request, ignore_params),
            "url": request.url,
            "headers": _headers(request.headers),
            "body": base64.b64encode(request.body).decode("ascii"),
            "body_encoding": "base64",
        },
    }


class CaptureMiddleware:
    def __init__(self, recorder: RunRecorder) -> None:
        self.recorder = recorder
        self._capture_nos = itertools.count(1)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler.settings[RECORDER])

    def process_request(self, request: Request) -> None:
        if PAGE_TYPE in request.meta:
            request.meta.pop(CAPTURE, None)
            request.meta[_STARTED] = (datetime.now(UTC).isoformat(), time.monotonic())

    def process_response(self, request: Request, response: Response) -> Response:
        if PAGE_TYPE not in request.meta:  # Scrapy's robots.txt request isn't a Page Type.
            return response
        capture_no = next(self._capture_nos)
        self.recorder.stats.add_response(response.status)
        started_at, started = request.meta[_STARTED]
        page_type = request.meta[PAGE_TYPE]
        meta = {
            "capture_no": capture_no,
            **request_metadata(request, self.recorder.ignore_params),
            "response": {
                "url": response.url,
                "status": response.status,
                "headers": _headers(response.headers),
                "timing": {
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": time.monotonic() - started,
                },
            },
        }
        capture = Capture(capture_no, page_type, self.recorder.redact_metadata(meta), response.body)
        request.meta[CAPTURE] = capture
        # Redirects and retries copy meta: the next exchange descends from this one.
        request.meta[PARENT_CAPTURE] = capture_no
        self.recorder.record(capture, {})
        return response


class RefreshStatusMiddleware:
    """Ordered above RetryMiddleware: retrying a refresh status would resend the stale Session."""

    def process_response(self, request: Request, response: Response) -> Response:
        if response.status in request.meta.get(REFRESH_ON, ()):
            request.meta["dont_retry"] = True
        return response


class SharedRobotsTxtMiddleware(RobotsTxtMiddleware):
    """Scrapy's awaits one shared Deferred per host, but awaiting it resets its result to None.

    So the second request queued behind the robots.txt download read "no robots.txt" and was
    sent; reading the parser back after the wait gives every request the real one.
    """

    async def robot_parser(self, request: Request) -> RobotParser | None:
        await super().robot_parser(request)
        parser = self._parsers[urlparse_cached(request).netloc]
        return None if isinstance(parser, Deferred) else parser
