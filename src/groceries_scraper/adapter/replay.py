"""Replay: serve a prior Run's Captures instead of the network."""

import base64
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from scrapy import Request
from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response
from scrapy.http.headers import Headers
from scrapy.responsetypes import responsetypes

from groceries_scraper.adapter.fingerprint import request_fingerprint
from groceries_scraper.adapter.middlewares import PAGE_TYPE, request_metadata
from groceries_scraper.adapter.settings import RECORDER, REPLAY
from groceries_scraper.run import Run
from groceries_scraper.run.directory import read_manifest
from groceries_scraper.run.recording import RunRecorder
from groceries_scraper.run.redaction import REDACTED

_REDACTED_MARKERS = (REDACTED, quote(REDACTED))


# Bodies of loaded Runs are read when served, not held in memory.
type Body = bytes | Path


class ReplayError(Exception):
    pass


class ReplayIndex:
    """Captures by request fingerprint, served in Capture order; the last one repeats."""

    def __init__(
        self, captures: Iterable[tuple[dict[str, Any], Body]], ignore_params: frozenset[str]
    ) -> None:
        self.ignore_params = ignore_params
        self._captures: dict[str, deque[tuple[dict[str, Any], Body]]] = {}
        for meta, body in captures:
            self._captures.setdefault(self._fingerprint(meta), deque()).append((meta, body))

    def _fingerprint(self, meta: dict[str, Any]) -> str:
        request = meta["request"]
        if "fingerprint" in request:
            fingerprint: str = request["fingerprint"]
            return fingerprint
        url = request["url"]
        if any(marker in url for marker in _REDACTED_MARKERS):
            raise ReplayError(f"Capture {meta.get('capture_no')} has a redacted URL: {url}")
        original = Request(
            url,
            method=request["method"],
            headers={"Content-Type": request["headers"].get("Content-Type", [])},
            body=base64.b64decode(request["body"]),
        )
        return request_fingerprint(original, self.ignore_params)

    def serve(self, request: Request) -> Response | None:
        queue = self._captures.get(request_fingerprint(request, self.ignore_params))
        if not queue:
            return None
        meta, source = queue.popleft() if len(queue) > 1 else queue[0]
        body = source.read_bytes() if isinstance(source, Path) else source
        # Sensitive headers are stored as a bare REDACTED string rather than a list.
        headers = Headers(
            {
                name: values
                for name, values in meta["response"]["headers"].items()
                if values != REDACTED
            }
        )
        # Redacted Capture URLs are display-only: the request's own URL is the original.
        cls = responsetypes.from_args(headers=headers, url=request.url, body=body)
        return cls(
            url=request.url,
            status=meta["response"]["status"],
            headers=headers,
            body=body,
            request=request,
            flags=["replay"],
        )


@dataclass(frozen=True)
class SourceRun:
    run: Run
    config: dict[str, Any]
    index: ReplayIndex

    @classmethod
    def load(cls, path: Path) -> Self:
        if not (path / "run.json").is_file():
            raise ReplayError(f"{path} is not a Run directory: no run.json")
        try:
            return cls._load(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ReplayError(f"Run {path} is unreadable: {type(exc).__name__}: {exc}") from None

    @classmethod
    def _load(cls, path: Path) -> Self:
        manifest = read_manifest(path)
        config = manifest["config"]
        level = config["settings"]["record_level"]
        if level != "all":
            raise ReplayError(f"Run {path} kept only some Captures (record_level: {level})")
        ignore = frozenset(config.get("replay", {}).get("ignore_params", []))
        captures: list[tuple[dict[str, Any], Body]] = []
        for meta_path in (path / "captures").glob("*.meta.json"):
            body = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".body")
            if not body.is_file():
                raise ReplayError(f"Capture {meta_path} has no body file")
            captures.append((json.loads(meta_path.read_text(encoding="utf-8")), body))
        # File names stop sorting numerically past 9999 Captures.
        captures.sort(key=lambda capture: int(capture[0]["capture_no"]))
        run = Run(manifest["site"], manifest["run_id"], path)
        return cls(run, config, ReplayIndex(captures, ignore))


class ReplayMiddleware:
    """Unmatched requests are recorded as missing and never reach the downloader."""

    def __init__(self, index: ReplayIndex, recorder: RunRecorder) -> None:
        self.index = index
        self.recorder = recorder

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler.settings[REPLAY], crawler.settings[RECORDER])

    def process_request(self, request: Request) -> Response:
        response = self.index.serve(request)
        if response is None:
            if PAGE_TYPE in request.meta:
                # The fingerprint looked up, under the source Run's ignore policy.
                self.recorder.record_missing(request_metadata(request, self.index.ignore_params))
            raise IgnoreRequest(f"Replay: no Capture matches {request.method} {request.url}")
        return response
