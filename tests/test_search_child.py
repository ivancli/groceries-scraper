"""The real crawler child, supervised, against a fake retailer on localhost."""

import asyncio
import os
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from groceries_scraper.coordination import Coordinator, CoordinatorAdmin, Policy
from groceries_scraper.search.adapters import SearchAdapter, load_adapter
from groceries_scraper.search.supervisor import Outcome, SearchRequest, Supervisor

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROLE = "scrape_coordinator_test"
PASSWORD = "coordinator-test"

TILE = '<article><h2>{title}</h2><a href="/p/{sku}">view</a></article>'
TILES = {
    "milk": [("Milk 2L", 1), ("Milk 1L", 2), ("Milk 2L", 1), ("Lactose Free Milk", 3), ("Oat", 4)],
    # A tile whose required title is missing.
    "broken": [("Bread", 5), ("", 6)],
}
# The image and font are retailer requests a search never needs.
RENDERED = """<html><head><style>@font-face {font-family: f; src: url('/f.woff2')}
body {font-family: f}</style></head><body><img src="/logo.png">
<main id="results"></main><script>
fetch('/api/tiles').then(r => r.text()).then(html => {
  document.getElementById('results').innerHTML = html;
});</script></body></html>"""


def _results_page(query: str) -> str:
    tiles = "".join(
        TILE.format(title=f"<span>{t}</span>" if t else "", sku=s) for t, s in TILES[query]
    )
    return f'<html><body><main id="results">{tiles}</main></body></html>'


PAGES = {
    "nothing": '<html><body><main id="results"><p class="none">No results</p></main></body></html>',
    "unverified": '<html><body><main id="results"></main></body></html>',
    "odd": "<html><body><p>Something else entirely</p></body></html>",
    "challenge": "<html><head><title>Pardon Our Interruption</title></head></html>",
}


@pytest.fixture
def retailer() -> Iterator[tuple[int, Counter[str]]]:
    hits: Counter[str] = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = parse_qs(url.query).get("q", [""])[0]
            hits[url.path if url.path != "/search" else f"/search?q={query}"] += 1
            if url.path == "/robots.txt":
                self._send(200, "text/plain", "User-agent: *\nAllow: /\n")
            elif url.path == "/api/tiles":
                self._send(200, "text/html", _results_page("milk").split('"results">')[1])
            elif url.path != "/search":
                self.send_error(404)
            elif query in TILES:
                self._send(200, "text/html", _results_page(query))
            elif query == "rendered":
                self._send(200, "text/html", RENDERED)
            elif query == "refused":
                self._send(403, "text/html", "<html><body>Forbidden</body></html>")
            elif query == "flaky":
                self._send(503, "text/html", "<html><body>Try again</body></html>")
            else:
                self._send(200, "text/html", PAGES[query])

        def _send(self, status: int, content_type: str, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], hits
    server.shutdown()
    server.server_close()


def _adapter(tmp_path: Path, port: int, render: str = "http") -> SearchAdapter:
    path = tmp_path / f"shop-{render}.yaml"
    path.write_text(f"""
search:
  adapter_version: shop-live-v1
  retailer: {{key: shop, name: Shop, hosts: ["127.0.0.1"]}}
  url: "http://127.0.0.1:{port}/search?q={{query}}"
  page_type: search
  record: result
  max_results: 3
  verify:
    page: "//main[@id='results']"
    empty: "//main[@id='results']/p[@class='none']"
    blocked: "//title[contains(., 'Pardon Our Interruption')]"
settings: {{download_delay: 0.2, concurrent_requests_per_domain: 2}}
records: {{result: {{key: [url]}}}}
page_types:
  search:
    render: {render}
    record: result
    items: {{each: {{css: "main#results article"}}}}
    fields:
      title: {{xpath: ".//h2/span/text()", type: string, required: true}}
      url: {{xpath: "concat('https://shop.example', .//a/@href)", type: string, required: true}}
""")
    return load_adapter(path)


@pytest.fixture
def coordinator() -> Iterator[Coordinator]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS coordination CASCADE")
    admin = CoordinatorAdmin(DSN)
    admin.prepare(ROLE, PASSWORD)
    admin.set_policies({"shop": Policy(0.2, 2)})
    coordinator = Coordinator(make_conninfo(DSN, user=ROLE, password=PASSWORD))
    yield coordinator
    coordinator.close()


def _search(
    coordinator: Coordinator, adapter: SearchAdapter, tmp_path: Path, query: str
) -> Outcome:
    service = Supervisor(coordinator, {"shop": adapter}, "api-a", workdir=tmp_path)
    request = SearchRequest(
        request_id=str(uuid.uuid4()),
        execution_key=f"owner-1:{query}",
        retailer="shop",
        adapter_version="shop-live-v1",
        query=query,
        context={"location": "default"},
        budget_ms=10_000,
        admission_budget_ms=2_000,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )

    async def go() -> Outcome:
        try:
            return await service.search(request)
        finally:
            await service.idle()

    return asyncio.run(go())


def _permits() -> tuple[int, int]:
    """Grants so far, and permits still held."""
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        granted = connection.execute(
            "SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM coordination.permits_id_seq"
        ).fetchone()
        held = connection.execute("SELECT count(*) FROM coordination.permits").fetchone()
    assert granted is not None and held is not None
    return int(granted[0]), int(held[0])


def test_results_keep_the_retailers_order_without_duplicates_up_to_the_limit(
    retailer: tuple[int, Counter[str]], coordinator: Coordinator, tmp_path: Path
) -> None:
    port, hits = retailer

    outcome = _search(coordinator, _adapter(tmp_path, port), tmp_path, "milk")

    assert outcome.status == "ok", outcome
    assert outcome.results == [
        {"title": "Milk 2L", "url": "https://shop.example/p/1"},
        {"title": "Milk 1L", "url": "https://shop.example/p/2"},
        {"title": "Lactose Free Milk", "url": "https://shop.example/p/3"},
    ]
    # robots.txt and the search page, each through a permit the supervisor granted.
    assert hits == {"/robots.txt": 1, "/search?q=milk": 1}
    assert _permits() == (2, 0)


def test_a_positively_verified_empty_page_is_an_empty_success(
    retailer: tuple[int, Counter[str]], coordinator: Coordinator, tmp_path: Path
) -> None:
    outcome = _search(coordinator, _adapter(tmp_path, retailer[0]), tmp_path, "nothing")

    assert (outcome.status, outcome.results) == ("ok", [])


@pytest.mark.parametrize(
    ("query", "status", "error"),
    [
        # No tiles, but nothing shows the search found none either.
        ("unverified", "failed", "unverified_empty"),
        ("odd", "failed", "unrecognized_page"),
        ("broken", "failed", "corrupt_result"),
        ("challenge", "blocked", None),
        ("refused", "blocked", None),
        ("flaky", "failed", "http_503"),
    ],
)
def test_only_a_verified_page_can_be_empty(
    retailer: tuple[int, Counter[str]],
    coordinator: Coordinator,
    tmp_path: Path,
    query: str,
    status: str,
    error: str | None,
) -> None:
    port, hits = retailer

    outcome = _search(coordinator, _adapter(tmp_path, port), tmp_path, query)

    assert (outcome.status, outcome.error, outcome.results) == (status, error, [])
    # One attempt: neither Scrapy nor a Session refresh retries a search.
    assert hits[f"/search?q={query}"] == 1


def test_a_browser_rendered_search_takes_permits_for_its_own_requests(
    retailer: tuple[int, Counter[str]], coordinator: Coordinator, tmp_path: Path
) -> None:
    port, hits = retailer

    outcome = _search(coordinator, _adapter(tmp_path, port, "browser"), tmp_path, "rendered")

    assert outcome.status == "ok", outcome
    assert [result["title"] for result in outcome.results] == [
        "Milk 2L",
        "Milk 1L",
        "Lactose Free Milk",
    ]
    assert hits["/api/tiles"] == 1
    assert hits["/logo.png"] == hits["/f.woff2"] == 0
    granted, held = _permits()
    assert (granted, held) == (sum(hits.values()), 0)
