"""Downloader middleware keeping Scrapy's retries out of Session refresh."""

from scrapy import Request
from scrapy.http import Response

# Request meta: the statuses the spider answers with a Session refresh (page requests only).
REFRESH_ON = "groceries_refresh_on"


class RefreshStatusMiddleware:
    """Ordered above RetryMiddleware: retrying a refresh status would resend the stale Session."""

    def process_response(self, request: Request, response: Response) -> Response:
        if response.status in request.meta.get(REFRESH_ON, ()):
            request.meta["dont_retry"] = True
        return response
