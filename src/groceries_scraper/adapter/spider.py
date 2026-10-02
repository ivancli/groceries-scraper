"""One generic spider: every response goes through the engine; Follow Requests go back out."""

import itertools
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import scrapy
from scrapy.http import Response

from groceries_scraper.adapter.pipelines import EmittedRecord
from groceries_scraper.config import Site
from groceries_scraper.engine.follow import FollowRequest
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext


class SiteSpider(scrapy.Spider):
    name = "site"  # replaced by the Site's name

    def __init__(self, site: Site, **kwargs: Any) -> None:
        super().__init__(name=site.site, **kwargs)
        self.site = site
        self.env = dict(os.environ)
        self._capture_nos = itertools.count(1)

    async def start(self) -> AsyncIterator[scrapy.Request]:
        for start in self.site.start:
            yield self._request(
                FollowRequest("GET", start.url, {}, None, start.page_type, {}, None)
            )

    def _request(self, follow: FollowRequest) -> scrapy.Request:
        return scrapy.Request(
            follow.url,
            method=follow.method,
            headers=follow.headers,
            body=follow.body,
            callback=self._on_response,
            cb_kwargs={"page_type": follow.page_type, "variables": follow.variables},
        )

    def _on_response(
        self, response: Response, page_type: str, variables: dict[str, Any]
    ) -> Iterator[EmittedRecord | scrapy.Request]:
        capture_no = next(self._capture_nos)
        content_type = response.headers.get("Content-Type")
        result = evaluate_response(
            self.site.page_types[page_type],
            response.body,
            content_type.decode("latin-1") if content_type else None,
            PipeContext(variables=variables, env=self.env, url=response.url),
            parent_ref=capture_no,
        )
        for record in result.extraction.records:
            yield EmittedRecord(record, response.url, capture_no)
        for follow in result.follow.requests:
            yield self._request(follow)
