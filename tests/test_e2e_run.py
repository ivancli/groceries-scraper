"""`scrape run` against a local listing → product site (HTML listing, JSON API)."""

import json
import secrets
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

PAGES = 3
PER_PAGE = 2
SCRAPE = Path(sys.executable).parent / "scrape"

SITE = Template("""
site: e2e
settings: {download_delay: 0, concurrent_requests_per_domain: 1$settings}
$session
records: {product: {key: [sku]}}
start: [{url: "$base/c/dairy", page_type: listing}]
page_types:
  listing:
    items: {each: {css: div.tile}}
    follow:
      - select: {css: "a::attr(href)"}
        scope: each
        page_type: product
        pass:
          sku: {css: "::attr(data-sku)"}
          price: {css: ".price::text"}
        request:
          method: POST
          url: "$base/api/product"
          json: {sku: "{{ sku }}"}
          $headers
      - select: {css: "a.next::attr(href)"}
        page_type: listing
  product:
    response: json
    record: product
    fields:
      sku: {var: sku, type: string, required: true}
      price: {var: price, type: number}
      name: {jsonpath: $$.name, type: string}
""")


class _Shop(BaseHTTPRequestHandler):
    server: "_ShopServer"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        self.server.paths.append(self.path)
        if url.path == "/robots.txt":
            self._send("text/plain", self.server.robots)
        elif url.path == "/" and self.server.home_redirects:
            self.send_response(302)
            self.send_header("Location", "/home")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == ("/home" if self.server.home_redirects else "/"):
            self._home()
        elif url.path == "/c/dairy":
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            self._send("text/html; charset=utf-8", _listing(page))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self.server.paths.append(self.path)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.server.csrf and not self._valid_token():
            self._send("text/plain", "token mismatch", status=419)
            return
        self.server.served += 1
        if self.server.rotate_every and self.server.served % self.server.rotate_every == 0:
            self.server.tokens.clear()
        self._send("application/json", json.dumps({"name": f"Product {body['sku']}"}))

    def _home(self) -> None:
        if self.server.home_status != 200:
            self.send_error(self.server.home_status)
            return
        sid, token = secrets.token_hex(4), secrets.token_hex(4)
        self.server.tokens[sid] = token
        page = f'<html><head><meta name="csrf-token" content="{token}"></head></html>'
        self._send("text/html", page, cookie=f"sid={sid}")

    def _valid_token(self) -> bool:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookies["sid"].value if "sid" in cookies else None
        token = self.headers.get("X-CSRF-Token")
        return token is not None and self.server.tokens.get(sid or "") == token

    def _send(
        self, content_type: str, body: str, status: int = 200, cookie: str | None = None
    ) -> None:
        data = body.encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _ShopServer(ThreadingHTTPServer):
    robots = ""
    paths: list[str]
    csrf = False  # the API needs the homepage's CSRF token for the sid cookie
    tokens: dict[str, str]  # sid -> CSRF token
    rotate_every = 0  # invalidate every token after this many products served
    served = 0
    home_status = 200
    home_redirects = False  # `/` → `/home`, as localised homepages often do


def _skus() -> list[str]:
    return [f"p{page}{i}" for page in range(1, PAGES + 1) for i in range(PER_PAGE)]


def _listing(page: int) -> str:
    tiles = "".join(
        f'<div class="tile" data-sku="p{page}{i}"><a href="/p/p{page}{i}">P</a>'
        f'<span class="price">{page}.{i}0</span></div>'
        for i in range(PER_PAGE)
    )
    more = f'<a class="next" href="/c/dairy?page={page + 1}">next</a>' if page < PAGES else ""
    return f"<html><body>{tiles}{more}</body></html>"


@pytest.fixture
def shop() -> Iterator[_ShopServer]:
    server = _ShopServer(("127.0.0.1", 0), _Shop)
    server.paths = []
    server.tokens = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


SESSION = Template("""
session:
  setup:
    - request: {url: "$base/"}
      extract:
        csrf: {css: "meta[name=csrf-token]::attr(content)"}
  refresh_on: [419]
  max_refresh: $max_refresh
""")
CSRF_HEADER = 'headers: {X-CSRF-Token: "{{ session.csrf }}"}'


def _run(tmp_path: Path, shop: _ShopServer, *args: str, settings: str = "") -> Path:
    return _run_with_log(tmp_path, shop, *args, settings=settings)[0]


def _run_with_log(
    tmp_path: Path,
    shop: _ShopServer,
    *args: str,
    settings: str = "",
    max_refresh: int | None = None,
) -> tuple[Path, str]:
    """`max_refresh` set: the Site runs Session Setup and sends the CSRF header."""
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    session = "" if max_refresh is None else SESSION.substitute(base=base, max_refresh=max_refresh)
    headers = "" if max_refresh is None else CSRF_HEADER
    config = tmp_path / "e2e.yaml"
    config.write_text(
        SITE.substitute(base=base, settings=settings, session=session, headers=headers)
    )
    result = subprocess.run(
        [str(SCRAPE), "run", str(config), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    [run_dir] = (tmp_path / "runs" / "e2e").iterdir()
    return run_dir, result.stderr


def _records(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "records" / "product.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_writes_product_records_with_meta(tmp_path: Path, shop: _ShopServer) -> None:
    run_dir = _run(tmp_path, shop)

    records = sorted(_records(run_dir), key=lambda r: r["sku"])
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    assert [{k: v for k, v in r.items() if k != "_meta"} for r in records] == [
        {"sku": sku, "price": float(f"{sku[1]}.{sku[2]}0"), "name": f"Product {sku}"}
        for sku in _skus()
    ]
    assert len({r["_meta"]["capture_no"] for r in records}) == len(records)
    for record in records:
        meta = dict(record["_meta"])
        assert datetime.fromisoformat(meta.pop("scraped_at")).tzinfo is not None
        assert isinstance(meta.pop("capture_no"), int)
        assert meta == {
            "site": "e2e",
            "run_id": run_dir.name,
            "record_type": "product",
            "source_url": f"{base}/api/product",
        }


def test_limit_stops_the_crawl_after_n_records(tmp_path: Path, shop: _ShopServer) -> None:
    run_dir = _run(tmp_path, shop, "--limit", "1")

    assert len(_records(run_dir)) == 1
    assert shop.paths.count("/api/product") < len(_skus())


def test_robots_txt_disallow_is_respected_by_default(tmp_path: Path, shop: _ShopServer) -> None:
    shop.robots = "User-agent: *\nDisallow: /api/\n"

    run_dir = _run(tmp_path, shop)

    assert _records(run_dir) == []
    assert "/c/dairy?page=2" in shop.paths  # the crawl ran; only the API was skipped
    assert "/api/product" not in shop.paths


def test_a_site_can_override_robots_txt(tmp_path: Path, shop: _ShopServer) -> None:
    shop.robots = "User-agent: *\nDisallow: /api/\n"

    run_dir = _run(tmp_path, shop, settings=", obey_robots: false")

    assert len(_records(run_dir)) == len(_skus())


def test_session_setup_supplies_the_csrf_header(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf = True

    run_dir, _ = _run_with_log(tmp_path, shop, max_refresh=2)

    assert len(_records(run_dir)) == len(_skus())
    assert shop.paths[:2] == ["/robots.txt", "/"]  # Session Setup precedes Start Requests
    assert shop.paths.count("/") == 1


def test_a_rotated_token_refreshes_the_session_and_retries(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.rotate_every = True, 2  # 6 products: tokens die after the 2nd and 4th

    run_dir, log = _run_with_log(tmp_path, shop, max_refresh=2)

    assert sorted(r["sku"] for r in _records(run_dir)) == _skus()
    assert shop.paths.count("/") == 3
    assert "'session/refreshes': 2" in log


def test_exceeding_max_refresh_stops_refreshing_and_is_reported(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.rotate_every = True, 2

    run_dir, log = _run_with_log(tmp_path, shop, max_refresh=1)

    assert len(_records(run_dir)) == 4  # the tokens die again after the 4th product
    assert shop.paths.count("/") == 2
    assert "max_refresh (1) reached" in log
    assert "'session/refresh_exhausted': 2" in log


def test_session_setup_follows_redirects(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.home_redirects = True, True

    run_dir, _ = _run_with_log(tmp_path, shop, max_refresh=1)

    assert len(_records(run_dir)) == len(_skus())


def test_a_failed_session_setup_stops_the_run(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.home_status = True, 503

    _, log = _run_with_log(tmp_path, shop, max_refresh=1)

    assert "/c/dairy" not in shop.paths
    assert "Session Setup step 0 failed: Session Setup request got HTTP 503" in log
    assert "'finish_reason': 'session_setup_failed'" in log
