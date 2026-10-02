"""Capture HTTP exchanges before redirects/retries; keep retries out of Session refresh."""

import base64
import itertools
import time
from datetime import UTC, datetime
from typing import Self

from scrapy import Request
from scrapy.crawler import Crawler
from scrapy.http import Response
from scrapy.http.headers import Headers

from groceries_scraper.adapter.settings import RECORDER
from groceries_scraper.run.recording import Capture, RunRecorder

# Request meta: the statuses the spider answers with a Session refresh (page requests only).
REFRESH_ON = "groceries_refresh_on"
PAGE_TYPE = "groceries_page_type"
VARIABLES = "groceries_variables"
PARENT_CAPTURE = "groceries_parent_capture"
CAPTURE = "groceries_capture"
_STARTED = "groceries_capture_started"


def _headers(headers: Headers) -> dict[str, list[str]]:
    return {
        name.decode("latin-1"): [value.decode("latin-1") for value in headers.getlist(name)]
        for name in headers
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
        started_at, started = request.meta[_STARTED]
        page_type = request.meta[PAGE_TYPE]
        request_headers, response_headers = _headers(request.headers), _headers(response.headers)
        meta = {
            "capture_no": capture_no,
            "page_type": page_type,
            "parent_capture_no": request.meta.get(PARENT_CAPTURE),
            "variables": self.recorder.redact_variables(
                request.meta.get(VARIABLES, {}), request_headers, response_headers
            ),
            "request": {
                "method": request.method,
                "url": request.url,
                "headers": self.recorder.redact_headers(request_headers),
                "body": base64.b64encode(request.body).decode("ascii"),
                "body_encoding": "base64",
            },
            "response": {
                "url": response.url,
                "status": response.status,
                "headers": self.recorder.redact_headers(response_headers),
                "timing": {
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": time.monotonic() - started,
                },
            },
        }
        capture = Capture(capture_no, page_type, meta, response.body)
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
