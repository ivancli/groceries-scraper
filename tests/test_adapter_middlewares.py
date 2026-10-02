from scrapy import Request
from scrapy.http import Response

from groceries_scraper.adapter.middlewares import REFRESH_ON, RefreshStatusMiddleware

URL = "https://shop.example/api/product"


def test_a_refresh_status_is_not_retried_with_the_stale_session() -> None:
    request = Request(URL, meta={REFRESH_ON: [419, 503]})

    response = RefreshStatusMiddleware().process_response(request, Response(URL, status=503))

    assert response.status == 503
    assert request.meta["dont_retry"] is True


def test_other_statuses_and_requests_are_left_to_retry() -> None:
    page = Request(URL, meta={REFRESH_ON: [419]})
    setup = Request(URL)

    RefreshStatusMiddleware().process_response(page, Response(URL, status=503))
    RefreshStatusMiddleware().process_response(setup, Response(URL, status=419))

    assert "dont_retry" not in page.meta
    assert "dont_retry" not in setup.meta
