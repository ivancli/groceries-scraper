"""One generic spider: every response goes through the engine; Follow Requests go back out."""

import itertools
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import scrapy
from scrapy.http import Response

from groceries_scraper.adapter.pipelines import RecordItem
from groceries_scraper.config import Site
from groceries_scraper.engine.page import evaluate_response
from groceries_scraper.engine.pipe import PipeContext


class SiteSpider(scrapy.Spider):
    name = "site"  # replaced by the Site's name

    def __init__(self, site: Site, env: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(name=site.site, **kwargs)
        self.site = site
        self.env = dict(os.environ) if env is None else env
        self._capture_nos = itertools.count(1)

    async def start(self) -> AsyncIterator[scrapy.Request]:
        for start in self.site.start:
            yield self._request(start.url, start.page_type, {})

    def _request(
        self,
        url: str,
        page_type: str,
        variables: dict[str, Any],
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> scrapy.Request:
        return scrapy.Request(
            url,
            method=method,
            headers=headers,
            body=body,
            callback=self._on_response,
            cb_kwargs={"page_type": page_type, "variables": variables},
        )

    def _on_response(
        self, response: Response, page_type: str, variables: dict[str, Any]
    ) -> Iterator[RecordItem | scrapy.Request]:
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
            yield RecordItem(record, response.url, capture_no)
        for follow in result.follow.requests:
            yield self._request(
                follow.url,
                follow.page_type,
                follow.variables,
                follow.method,
                follow.headers,
                follow.body,
            )
