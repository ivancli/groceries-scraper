"""`scrape run` against a local listing → product site (HTML listing, JSON API)."""

import base64
import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
import yaml
from typer.testing import CliRunner

from groceries_scraper.cli import app

PAGES = 3
PER_PAGE = 2
SCRAPE = Path(sys.executable).parent / "scrape"

SITE = Template("""
site: e2e
settings: {download_delay: 0, concurrent_requests_per_domain: 1$settings}
$session
records: {product: {key: [sku]}}
$health
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


# Tiles only exist after the script's fetch, so only a browser render sees them.
JS_LISTING = """<html><body><div id="app"></div><script>
fetch("/api/tiles" + location.search)
  .then((response) => response.text())
  .then((html) => { document.getElementById("app").innerHTML = html; });
</script></body></html>"""


def _site_config(**values: str) -> str:
    return SITE.substitute({"health": ""}, **values)


class _Shop(BaseHTTPRequestHandler):
    server: "_ShopServer"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        self.server.paths.append(self.path)
        self.server.sids.append((self.path, self._sid()))
        if url.path == "/robots.txt":
            self._send("text/plain", self.server.robots)
        elif url.path == "/" and self.server.home_redirects:
            self.send_response(302)
            self.send_header("Location", "/home")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == ("/home" if self.server.home_redirects else "/"):
            self._home()
        elif url.path == "/c/dairy" and self.server.render_js:
            self._send("text/html; charset=utf-8", JS_LISTING)
        elif url.path.startswith("/c/") or url.path == "/api/tiles":
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            category = url.path.removeprefix("/c/") if url.path.startswith("/c/") else "dairy"
            listing = _listing(page, self.server.wrap_pagination, category)
            self._send("text/html; charset=utf-8", listing)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self.server.paths.append(self.path)
        self.server.sids.append((self.path, self._sid()))
        self.server.authorizations.append(self.headers.get("Authorization", ""))
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            body = {key: values[0] for key, values in parse_qs(data.decode()).items()}
        else:
            body = json.loads(data)
        self.server.bodies.append(body)
        if self.server.csrf and not self._valid_token():
            self._send("text/plain", "token mismatch", status=419)
            return
        self.server.served += 1
        if self.server.rotate_every and self.server.served % self.server.rotate_every == 0:
            self.server.tokens.clear()
        if body["sku"] in self.server.product_responses:
            status, payload = self.server.product_responses[body["sku"]]
            self._send("application/json", payload, status=status)
            return
        self._send("application/json", json.dumps({"name": f"Product {body['sku']}"}))

    def _home(self) -> None:
        if self.server.home_status != 200:
            self.send_error(self.server.home_status)
            return
        if self.server.home_ok_limit is not None:
            if self.server.home_ok_limit == 0:
                self.send_error(404)
                return
            self.server.home_ok_limit -= 1
        if self.server.home_failures:
            self.server.home_failures -= 1
            self.send_error(self.server.home_failure_status)
            return
        if self.server.session_payload is not None:
            self._send("application/json", json.dumps(self.server.session_payload))
            return
        sid, token = secrets.token_hex(4), secrets.token_hex(4)
        self.server.tokens[sid] = token
        page = f'<html><head><meta name="csrf-token" content="{token}"></head></html>'
        self._send("text/html", page, cookie=f"sid={sid}")

    def _sid(self) -> str | None:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        return cookies["sid"].value if "sid" in cookies else None

    def _valid_token(self) -> bool:
        sid = self._sid()
        token = self.headers.get("X-CSRF-Token")
        return token is not None and self.server.tokens.get(sid or "") == token

    def _send(
        self, content_type: str, body: str, status: int = 200, cookie: str | None = None
    ) -> None:
        data = body.encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for name, value in self.server.extra_headers:
            self.send_header(name, value)
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
    home_failures = 0  # the homepage's first N responses fail with `home_failure_status`
    home_failure_status = 503
    home_ok_limit: int | None = None  # the homepage 404s after this many responses
    wrap_pagination = False  # the last listing page links back to the first
    render_js = False  # listing pages are a script shell fetching their tiles
    product_responses: dict[str, tuple[int, str]]
    extra_headers: list[tuple[str, str]]
    session_payload: dict[str, Any] | None = None
    authorizations: list[str]
    sids: list[tuple[str, str | None]]  # (path, the request's `sid` cookie)
    bodies: list[dict[str, Any]]  # POST bodies


def _skus(category: str = "dairy") -> list[str]:
    """Dairy SKUs start with `p`; other categories' with their first letter."""
    prefix = "p" if category == "dairy" else category[0]
    return [f"{prefix}{page}{i}" for page in range(1, PAGES + 1) for i in range(PER_PAGE)]


def _listing(page: int, wrap: bool = False, category: str = "dairy") -> str:
    prefix = "p" if category == "dairy" else category[0]
    tiles = "".join(
        f'<div class="tile" data-sku="{prefix}{page}{i}"><a href="/p/{prefix}{page}{i}">P</a>'
        f'<span class="price">{page}.{i}0</span></div>'
        for i in range(PER_PAGE)
    )
    more = f'<a class="next" href="/c/{category}?page={page + 1}">next</a>' if page < PAGES else ""
    if wrap and page == PAGES:
        more = '<a class="next" href="/c/dairy">first</a>'
    return f"<html><body>{tiles}{more}</body></html>"


@pytest.fixture
def shop() -> Iterator[_ShopServer]:
    server = _ShopServer(("127.0.0.1", 0), _Shop)
    server.paths = []
    server.tokens = {}
    server.product_responses = {}
    server.extra_headers = []
    server.authorizations = []
    server.sids = []
    server.bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


SESSION = Template("""
session:
  pool: $pool
  setup:
    - request: {url: "$base/"}
      extract:
        csrf: {css: "meta[name=csrf-token]::attr(content)"}
        token_copy: {css: "meta[name=csrf-token]::attr(content)"}
        locale: {template: "en-AU"}
  refresh_on: [$refresh_on]
  max_refresh: $max_refresh
""")
CSRF_HEADER = 'headers: {X-CSRF-Token: "{{ session.csrf }}"}'


def _run(
    tmp_path: Path, shop: _ShopServer, *args: str, settings: str = "", exit_code: int = 0
) -> Path:
    return _run_with_log(tmp_path, shop, *args, settings=settings, exit_code=exit_code)[0]


def _run_with_log(
    tmp_path: Path,
    shop: _ShopServer,
    *args: str,
    settings: str = "",
    max_refresh: int | None = None,
    refresh_on: str = "419",
    request_headers: str | None = None,
    health: str = "",
    pool: int = 1,
    edit: Callable[[str], str] = lambda config: config,
    exit_code: int = 0,
) -> tuple[Path, str]:
    """`max_refresh` set: the Site runs Session Setup and sends the CSRF header."""
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    session = (
        ""
        if max_refresh is None
        else SESSION.substitute(
            base=base, max_refresh=max_refresh, refresh_on=refresh_on, pool=pool
        )
    )
    headers = (
        request_headers
        if request_headers is not None
        else ("" if max_refresh is None else CSRF_HEADER)
    )
    config = tmp_path / "e2e.yaml"
    config.write_text(
        edit(
            _site_config(
                base=base, settings=settings, session=session, headers=headers, health=health
            )
        )
    )
    return _crawl_config(tmp_path, *args, exit_code=exit_code)


def _crawl_config(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None, exit_code: int = 0
) -> tuple[Path, str]:
    result = subprocess.run(
        [str(SCRAPE), "run", str(tmp_path / "e2e.yaml"), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == exit_code, result.stderr
    [run_dir] = (tmp_path / "runs" / "e2e").iterdir()
    return run_dir, result.stderr + result.stdout


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
            "location": "default",
            "record_type": "product",
            "source_url": f"{base}/api/product",
        }


def _keyed_by_name(tmp_path: Path, shop: _ShopServer, settings: str = "") -> Path:
    """p11 repeats p10's name and p21 has none."""
    shop.product_responses = {
        "p11": (200, json.dumps({"name": "Product p10"})),
        "p21": (200, json.dumps({})),
    }
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = _site_config(base=base, settings=settings, session="", headers="")
    (tmp_path / "e2e.yaml").write_text(config.replace("key: [sku]", "key: [name]"))
    return _crawl_config(tmp_path)[0]


def test_records_with_a_repeated_or_missing_record_key_are_dropped(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _keyed_by_name(tmp_path, shop)

    names = [record["name"] for record in _records(run_dir)]
    assert sorted(names) == ["Product p10", "Product p20", "Product p30", "Product p31"]
    assert _manifest(run_dir)["stats"]["dropped"] == {
        "total": 2,
        "by_reason": {"duplicate Record Key": 1, "Record Key Field `name` is missing": 1},
    }
    missing, duplicate = sorted(
        entry["reason"]
        for trace in (run_dir / "traces").glob("*-product.trace.json")
        for entry in json.loads(trace.read_text())["dropped"]
    )
    assert missing == "Record Key Field `name` is missing"
    assert re.fullmatch(
        r'duplicate Record Key \{"name": "Product p10"\} \(first in Capture \d+\)', duplicate
    )


def test_errors_recording_keeps_missing_key_drops_but_not_duplicates(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _keyed_by_name(tmp_path, shop, settings=", record_level: errors")

    assert [capture["variables"]["sku"] for capture in _captures(run_dir)] == ["p21"]


def test_inspect_displays_html_and_json_captures_from_an_e2e_run(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _run(tmp_path, shop)
    runner = CliRunner()
    requests_before = list(shop.paths)
    for capture in _captures(run_dir):
        result = runner.invoke(
            app,
            ["inspect", str(run_dir), str(capture["capture_no"]), "--body"],
            env={"COLUMNS": "140"},
        )
        assert result.exit_code == 0, result.output
        assert "Extraction Trace" in result.stdout
        assert "HTTP 200" in result.stdout
        assert "Parent chain:" in result.stdout
        assert "Response body" in result.stdout
        if capture["page_type"] == "listing":
            assert "Follow Rule 0: select (Loop node 0)" in result.stdout
            assert "1. css ->" in result.stdout
            assert '<div class="tile"' in result.stdout
        else:
            assert "Field name (Record 0)" in result.stdout
            assert "1. jsonpath ->" in result.stdout
            assert '  "name": "Product p' in result.stdout
            assert "(listing) ->" in result.stdout
    assert shop.paths == requests_before


def test_limit_stops_the_crawl_after_n_records(tmp_path: Path, shop: _ShopServer) -> None:
    run_dir = _run(tmp_path, shop, "--limit", "1")

    assert len(_records(run_dir)) == 1
    assert shop.paths.count("/api/product") < len(_skus())


def test_robots_txt_disallow_is_respected_by_default(tmp_path: Path, shop: _ShopServer) -> None:
    shop.robots = "User-agent: *\nDisallow: /api/\n"

    run_dir = _run(tmp_path, shop, exit_code=2)  # no Records of a declared Record Type

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
    captures = _captures(run_dir)
    by_number = {capture["capture_no"]: capture for capture in captures}
    failures = [capture for capture in captures if capture["response"]["status"] == 419]
    assert failures
    failed_numbers = {capture["capture_no"] for capture in failures}
    retries = [capture for capture in captures if capture["parent_capture_no"] in failed_numbers]
    assert any(capture["page_type"] == "product" for capture in retries)
    for capture in retries:
        if capture["page_type"] == "product":
            parent = by_number[capture["parent_capture_no"]]
            assert capture["request"]["url"] == parent["request"]["url"]
            assert capture["variables"]["session"]["csrf"] != parent["variables"]["session"]["csrf"]


def test_exceeding_max_refresh_loses_the_only_session_and_fails_the_run(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.rotate_every = True, 2

    run_dir, log = _run_with_log(tmp_path, shop, max_refresh=1, exit_code=2)

    assert len(_records(run_dir)) == 4  # the tokens die again after the 4th product
    assert shop.paths.count("/") == 2
    assert "max_refresh (1) reached" in log
    assert "Session 1 lost: max_refresh reached" in log
    assert "'session/refresh_exhausted'" in log  # once more per request already in flight
    assert _manifest(run_dir)["health"]["breaches"] == [
        {"check": "finish_reason", "detail": SESSIONS_LOST}
    ]


def test_session_setup_follows_redirects(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.home_redirects = True, True

    run_dir, _ = _run_with_log(tmp_path, shop, max_refresh=1)

    assert len(_records(run_dir)) == len(_skus())
    setup = [capture for capture in _captures(run_dir) if capture["page_type"] == "session_setup"]
    assert [capture["response"]["status"] for capture in setup] == [302, 200]
    assert setup[0]["parent_capture_no"] is None
    assert setup[1]["parent_capture_no"] == setup[0]["capture_no"]


def test_start_requests_held_for_session_setup_are_still_deduplicated(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.wrap_pagination = True, True

    _run_with_log(tmp_path, shop, max_refresh=1)

    assert shop.paths.count("/c/dairy") == 1


def test_session_setup_is_retried_even_for_a_refresh_status(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.home_failures = True, 1

    run_dir, _ = _run_with_log(tmp_path, shop, max_refresh=1, refresh_on="419, 503")

    assert len(_records(run_dir)) == len(_skus())
    assert shop.paths.count("/") == 2
    setup = [capture for capture in _captures(run_dir) if capture["page_type"] == "session_setup"]
    assert [capture["response"]["status"] for capture in setup] == [503, 200]
    assert setup[1]["parent_capture_no"] == setup[0]["capture_no"]


def test_a_failed_session_setup_stops_the_run(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.home_status = True, 503

    run_dir, log = _run_with_log(tmp_path, shop, max_refresh=1, exit_code=2)

    assert _manifest(run_dir)["health"] == {
        "level": "failed",
        "breaches": [
            {"check": "finish_reason", "detail": SESSIONS_LOST},
            {"check": "records.product", "detail": "no Records"},
        ],
    }
    assert "/c/dairy" not in shop.paths
    assert "Session Setup step 0 failed: Session Setup request got HTTP 503" in log
    assert "'finish_reason': 'session_setup_failed'" in log


SESSIONS_LOST = "every Session was lost (Session Setup failed or max_refresh reached)"


def _manifest(run_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((run_dir / "run.json").read_text())
    return manifest


def test_a_healthy_run_saves_stats_and_health_and_exits_0(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.product_responses = {"p10": (404, "{}"), "p11": (200, "{}")}

    run_dir, log = _run_with_log(tmp_path, shop, health="health: {min_records: {product: 5}}")

    manifest = _manifest(run_dir)
    stats = manifest["stats"]
    assert stats["duration_seconds"] > 0
    assert {key: value for key, value in stats.items() if key != "duration_seconds"} == {
        "finish_reason": "finished",
        "pages": {"listing": 3, "product": 5},  # HTTP errors never reach a Page Type
        "records": {"product": 5},
        "dropped": {"total": 0, "by_reason": {}},
        "null_ratio": {"product": {"name": 1 / 5, "price": 0.0, "sku": 0.0}},
        "requests": {"ok": 8, "failed": {"HTTP 404": 1}, "missing": 0},
        "http_status": {"200": 8, "404": 1},
        "sessions": {"pool": 1, "lost": 0},
    }
    assert manifest["health"] == {"level": "ok", "breaches": []}
    assert "records: product 5" in log
    assert "health: ok" in log


def test_a_health_check_breach_degrades_the_run_and_exits_1(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.product_responses = {"p10": (200, "{}")}

    run_dir, log = _run_with_log(
        tmp_path, shop, health="health: {max_null_ratio: {name: 0.1}}", exit_code=1
    )

    breach = "max_null_ratio.name: 0.167 > 0.100 in `product`"
    assert _manifest(run_dir)["health"] == {
        "level": "degraded",
        "breaches": [{"check": "max_null_ratio.name", "detail": breach.split(": ", 1)[1]}],
    }
    assert f"health: degraded\n  {breach}" in log


def test_a_run_cut_short_by_the_limit_skips_record_count_checks(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _run_with_log(
        tmp_path, shop, "--limit", "1", health="health: {min_records: {product: 6}}"
    )[0]

    assert _manifest(run_dir)["health"] == {"level": "ok", "breaches": []}


def _captures(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text()) for path in sorted((run_dir / "captures").glob("*.meta.json"))
    ]


def test_run_captures_link_records_and_traces_to_their_parent_page(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _run(tmp_path, shop)
    captures = _captures(run_dir)
    assert [capture["capture_no"] for capture in captures] == list(range(1, 10))
    by_number = {capture["capture_no"]: capture for capture in captures}
    for capture in captures:
        number, page_type = capture["capture_no"], capture["page_type"]
        stem = f"{number:04d}-{page_type}"
        trace = json.loads((run_dir / "traces" / f"{stem}.trace.json").read_text())
        body = (run_dir / "captures" / f"{stem}.body").read_bytes()
        assert (trace["capture_no"], trace["page_type"]) == (number, page_type)
        assert capture["response"]["status"] == 200
        timing = capture["response"]["timing"]
        assert datetime.fromisoformat(timing["started_at"]) <= datetime.fromisoformat(
            timing["finished_at"]
        )
        assert timing["elapsed_seconds"] >= 0
        parent = capture["parent_capture_no"]
        if number == 1:
            assert parent is None
        else:
            assert parent < number
            assert by_number[parent]["page_type"] == "listing"
        if page_type == "listing":
            assert capture["request"]["method"] == "GET"
            assert trace["loop"][0]["step"] == "css"
            assert any(entry["path"] == "pass.price" for entry in trace["follow"])
            assert b'div class="tile"' in body
        else:
            sku = capture["variables"]["sku"]
            assert capture["request"]["method"] == "POST"
            assert capture["request"]["body_encoding"] == "base64"
            assert json.loads(base64.b64decode(capture["request"]["body"])) == {"sku": sku}
            assert json.loads(body) == {"name": f"Product {sku}"}
            name_trace = next(entry for entry in trace["fields"] if entry["path"] == "name")
            assert name_trace["steps"] == [
                {"step": "jsonpath", "output": [f"Product {sku}"], "error": None}
            ]
    for record in _records(run_dir):
        capture = by_number[record["_meta"]["capture_no"]]
        assert capture["page_type"] == "product"
        assert capture["variables"]["sku"] == record["sku"]


def test_errors_recording_keeps_only_http_and_extraction_failures(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.product_responses = {
        "p10": (422, '{"error": "unavailable"}'),
        "p20": (200, '{"name": {"unexpected": "object"}}'),
        "p30": (200, "invalid JSON"),
    }
    run_dir = _run(tmp_path, shop, settings=", record_level: errors")
    captures = _captures(run_dir)
    assert {capture["variables"]["sku"] for capture in captures} == {"p10", "p20", "p30"}
    assert sorted(capture["response"]["status"] for capture in captures) == [200, 200, 422]
    assert all(capture["page_type"] == "product" for capture in captures)
    assert len(list((run_dir / "captures").glob("*.body"))) == 3
    assert len(list((run_dir / "traces").glob("*.trace.json"))) == 3
    for capture in captures:
        trace = json.loads(
            (run_dir / "traces" / (f"{capture['capture_no']:04d}-product.trace.json")).read_text()
        )
        if capture["variables"]["sku"] == "p20":
            assert any(entry["error"] for entry in trace["fields"])
        if capture["variables"]["sku"] == "p30":
            assert trace["error"].startswith("JSONDecodeError:")


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [("all", "off", "off"), ("off", "all", "all"), ("all", "errors", "errors")],
)
def test_cli_record_override_is_saved_and_controls_capture_output(
    tmp_path: Path, shop: _ShopServer, configured: str, override: str, expected: str
) -> None:
    run_dir = _run(tmp_path, shop, "--record", override, settings=f', record_level: "{configured}"')
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["config"]["settings"]["record_level"] == expected
    assert len(_records(run_dir)) == 6
    if expected == "all":
        assert len(_captures(run_dir)) == 9
    else:
        assert not (run_dir / "captures").exists()
        assert not (run_dir / "traces").exists()


def test_sensitive_headers_and_their_session_variables_are_absent_from_metadata(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf = True
    shop.extra_headers = [
        ("x-api-secret", "response-secret"),
        ("X-Public", "first"),
        ("X-Public", "second"),
    ]
    run_dir, _ = _run_with_log(
        tmp_path,
        shop,
        max_refresh=2,
        settings=", redact_headers: [x-csrf-token, X-API-SECRET]",
        request_headers='headers: {Authorization: "Bearer auth-secret", '
        'X-CSRF-Token: "{{ session.csrf }}", x-api-secret: "request-secret"}',
    )
    captures = _captures(run_dir)
    meta_text = "\n".join(path.read_text() for path in (run_dir / "captures").glob("*.meta.json"))
    for secret in [
        "auth-secret",
        "request-secret",
        "response-secret",
        *shop.tokens,
        *shop.tokens.values(),
    ]:
        assert secret not in meta_text
    setup = next(capture for capture in captures if capture["page_type"] == "session_setup")
    assert setup["response"]["headers"]["Set-Cookie"] == "[REDACTED]"
    product = next(capture for capture in captures if capture["page_type"] == "product")
    for header in ("Authorization", "Cookie", "X-Csrf-Token", "X-Api-Secret"):
        assert product["request"]["headers"][header] == "[REDACTED]"
    assert product["response"]["headers"]["X-Api-Secret"] == "[REDACTED]"
    assert product["response"]["headers"]["X-Public"] == ["first", "second"]
    assert product["variables"]["session"] == {
        "csrf": "[REDACTED]",
        "token_copy": "[REDACTED]",
        "locale": "en-AU",
    }
    listing = next(capture for capture in captures if capture["page_type"] == "listing")
    assert listing["variables"]["session"] == {
        "csrf": "[REDACTED]",
        "token_copy": "[REDACTED]",
        "locale": "en-AU",
    }
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["config"]["page_types"]["listing"]["follow"][0]["request"]["headers"] == {
        "Authorization": "[REDACTED]",
        "X-CSRF-Token": "[REDACTED]",
        "x-api-secret": "[REDACTED]",
    }


def test_custom_step_values_can_be_recorded_without_interrupting_extraction(
    tmp_path: Path, shop: _ShopServer
) -> None:
    (tmp_path / "transforms.py").write_text(
        "from decimal import Decimal\ndef decimal(value, ctx):\n    return Decimal(value)\n"
    )
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = tmp_path / "e2e.yaml"
    config.write_text(f"""
site: e2e
settings: {{download_delay: 0}}
records: {{product: {{}}}}
start: [{{url: "{base}/c/dairy", page_type: listing}}]
page_types:
  listing:
    record: product
    fields:
      price:
        type: number
        <pipe>:
          - {{css: ".price::text"}}
          - {{fn: "transforms:decimal"}}
          - {{template: "{{{{ value }}}}"}}
""")
    run_dir, _ = _crawl_config(tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)})
    assert [record["price"] for record in _records(run_dir)] == [1.0]
    trace = json.loads((run_dir / "traces" / "0001-listing.trace.json").read_text())
    assert trace["fields"][0]["steps"][1] == {
        "step": "fn",
        "output": ["1.00", "1.10"],
        "error": None,
    }


def test_errors_recording_keeps_a_failed_step_in_a_follow_rule(
    tmp_path: Path, shop: _ShopServer
) -> None:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    (tmp_path / "e2e.yaml").write_text(f"""
site: e2e
settings: {{download_delay: 0, record_level: errors}}
start: [{{url: "{base}/c/dairy", page_type: listing}}]
page_types:
  listing:
    follow:
      - select: [{{css: "a::attr(href)"}}, {{parse: json}}]
        page_type: product
  product: {{}}
""")
    run_dir, _ = _crawl_config(tmp_path)
    [capture] = _captures(run_dir)
    assert (capture["page_type"], capture["response"]["status"]) == ("listing", 200)
    trace = json.loads((run_dir / "traces" / "0001-listing.trace.json").read_text())
    [entry] = trace["follow"]
    assert entry["error"] is None
    assert entry["steps"][1]["error"].startswith("JSONDecodeError:")


def test_errors_recording_does_not_treat_matched_data_as_a_trace_error(
    tmp_path: Path, shop: _ShopServer
) -> None:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    (tmp_path / "e2e.yaml").write_text(f"""
site: e2e
settings: {{download_delay: 0, record_level: errors}}
records: {{product: {{}}}}
start: [{{url: "{base}/c/dairy", page_type: listing}}]
page_types:
  listing:
    record: product
    fields:
      info: [{{template: '{{"error": "business data", "dropped": true}}'}}, {{parse: json}}]
""")
    run_dir, _ = _crawl_config(tmp_path)
    assert [record["info"] for record in _records(run_dir)] == [
        {"error": "business data", "dropped": True}
    ]
    assert _captures(run_dir) == []


@pytest.mark.parametrize("pin", [123456, 0, False, 98.5])
def test_numeric_session_secrets_and_their_copies_are_redacted(
    tmp_path: Path, shop: _ShopServer, pin: int | float | bool
) -> None:
    shop.session_payload = {
        "pin": pin,
        "copy": pin,
        "text_copy": str(pin),
        "nested": [pin, str(pin)],
        "count": 2,
        "active": True,
    }
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    session = f"""
session:
  setup:
    - request: {{url: "{base}/"}}
      extract:
        pin: {{jsonpath: $.pin}}
        copy: {{jsonpath: $.copy}}
        text_copy: {{jsonpath: $.text_copy}}
        nested: {{jsonpath: $.nested}}
        count: {{jsonpath: $.count}}
        active: {{jsonpath: $.active}}
"""
    (tmp_path / "e2e.yaml").write_text(
        _site_config(
            base=base,
            settings="",
            session=session,
            headers='headers: {Authorization: "Bearer {{ session.pin }}"}',
        )
    )
    run_dir, _ = _crawl_config(tmp_path)
    variables = [
        capture["variables"]["session"]
        for capture in _captures(run_dir)
        if capture["page_type"] != "session_setup"
    ]
    assert variables
    assert all(
        value
        == {
            "pin": "[REDACTED]",
            "copy": "[REDACTED]",
            "text_copy": "[REDACTED]",
            "nested": ["[REDACTED]", "[REDACTED]"],
            "count": 2,
            "active": True,
        }
        for value in variables
    )
    assert len(_records(run_dir)) == 6


@pytest.mark.parametrize("pin", [123456, 2, "s3cr+et/token?", "s3cr&et", "s3cr et"])
def test_known_secrets_are_redacted_from_capture_urls_without_changing_requests(
    tmp_path: Path, shop: _ShopServer, pin: int | str
) -> None:
    shop.session_payload = {"pin": pin}
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    session = f"""
session:
  setup:
    - request: {{url: "{base}/"}}
      extract:
        pin: {{jsonpath: $.pin}}
"""
    encoded = "{{ session.pin | string | urlencode | replace('/', '%2F') }}"
    template = (
        f"{base}/api/public%2Fsku/{encoded}/product?pin={encoded}&raw={{{{ session.pin }}}}"
        "&public=visible&blank="
    )
    content_location = (
        base.replace("http://", f"http://client:{quote(str(pin), safe='')}@")
        + f"/api/public%2Fsku/{quote(str(pin), safe='')}/product"
        f"?pin={quote(str(pin), safe='')}&public=visible#public%2Fsku/{quote(str(pin), safe='')}"
    )
    shop.extra_headers = [("Content-Location", content_location)]
    config = _site_config(
        base=base,
        settings="",
        session=session,
        headers='headers: {Authorization: "Bearer {{ session.pin }}", '
        f'Referer: "{template}"}}',
    ).replace(f'url: "{base}/api/product"', f'url: "{template}"')
    (tmp_path / "e2e.yaml").write_text(config)
    run_dir, log = _crawl_config(tmp_path)
    captures = [capture for capture in _captures(run_dir) if capture["page_type"] == "product"]
    assert len(captures) == len(_records(run_dir)) == 6, log
    for capture in captures:
        for side in ("request", "response"):
            recorded_url = capture[side]["url"]
            parsed = urlsplit(recorded_url)
            assert parsed.hostname == "127.0.0.1"
            assert parsed.port == shop.server_address[1]
            assert parsed.path == "/api/public%2Fsku/%5BREDACTED%5D/product"
            assert parse_qs(parsed.query, keep_blank_values=True) == {
                "pin": ["[REDACTED]"],
                "raw": ["[REDACTED]"],
                "public": ["visible"],
                "blank": [""],
            }
        content_url = urlsplit(capture["response"]["headers"]["Content-Location"][0])
        assert unquote(content_url.netloc.split("@", 1)[0]) == "[REDACTED]"
        assert content_url.path == "/api/public%2Fsku/%5BREDACTED%5D/product"
        assert content_url.fragment == "public%2Fsku/%5BREDACTED%5D"
        assert parse_qs(content_url.query) == {"pin": ["[REDACTED]"], "public": ["visible"]}
        referer = capture["request"]["headers"]["Referer"][0]
        assert referer == capture["request"]["url"]
    original_paths = [path for path in shop.paths if path.startswith("/api/")]
    assert len(original_paths) == 6
    assert all(str(pin) in unquote(path) for path in original_paths)
    for authorization in shop.authorizations:
        assert authorization == f"Bearer {pin}"
    for capture in _captures(run_dir):
        content_url = urlsplit(capture["response"]["headers"]["Content-Location"][0])
        assert content_url.path == "/api/public%2Fsku/%5BREDACTED%5D/product"
        assert content_url.fragment == "public%2Fsku/%5BREDACTED%5D"


@pytest.mark.parametrize("ignore", [False, True])
@pytest.mark.parametrize("location", ["query", "json", "form"])
def test_capture_fingerprints_use_original_requests_and_the_source_ignore_policy(
    tmp_path: Path, shop: _ShopServer, ignore: bool, location: str
) -> None:
    shop.session_payload = {"pin": 123456, "other_pin": 987654}
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = yaml.safe_load(_site_config(base=base, settings="", session="", headers=""))
    config["records"]["product"] = {}  # one Record per request, even for a repeated sku
    config["session"] = {
        "setup": [
            {
                "request": {"url": f"{base}/"},
                "extract": {
                    "pin": {"jsonpath": "$.pin"},
                    "other_pin": {"jsonpath": "$.other_pin"},
                },
            }
        ]
    }
    if ignore:
        config["replay"] = {"ignore_params": ["pin"]}
    first = config["page_types"]["listing"]["follow"][0]
    second = copy.deepcopy(first)
    for rule, variable in ((first, "pin"), (second, "other_pin")):
        request = rule["request"]
        token = f"{{{{ session.{variable} }}}}"
        request["headers"] = {"Authorization": f"Bearer {token}"}
        if location == "query":
            request["url"] += f"?pin={token}&public=visible"
        elif location == "json":
            request["json"]["pin"] = token
        else:
            request["form"] = {**request.pop("json"), "pin": token}
    config["page_types"]["listing"]["follow"].insert(1, second)
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    run_dir, _ = _crawl_config(tmp_path)
    captures = [capture for capture in _captures(run_dir) if capture["page_type"] == "product"]
    assert len(captures) == len(_records(run_dir)) == 12
    assert len({capture["request"]["fingerprint"] for capture in captures}) == (6 if ignore else 12)
    assert all(record["name"] == f"Product {record['sku']}" for record in _records(run_dir))
    for sku in _skus():
        pair = [capture["request"] for capture in captures if capture["variables"]["sku"] == sku]
        assert len(pair) == 2
        assert pair[0]["url"] == pair[1]["url"]
        for request in pair:
            assert re.fullmatch(r"scrapy-sha1-v1:[0-9a-f]{40}", request["fingerprint"])
        assert (pair[0]["fingerprint"] == pair[1]["fingerprint"]) is ignore


def test_form_fingerprints_decode_ignored_keys_and_public_values_using_declared_charset(
    tmp_path: Path, shop: _ShopServer
) -> None:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = yaml.safe_load(_site_config(base=base, settings="", session="", headers=""))
    config["records"]["product"] = {}  # one Record per request, even for a repeated sku
    config["replay"] = {"ignore_params": ["café", "pin"]}
    original = config["page_types"]["listing"]["follow"].pop(0)
    variants = [
        ('"ISO-8859-1"', "caf%E9=discard&name=caf%E9&pin=first"),
        ('"UTF-8"', "caf%C3%A9=discard&name=caf%C3%A9&pin=second"),
        ('"ISO-8859-1"', "name=caf%E9"),
    ]
    for charset, suffix in reversed(variants):
        rule = copy.deepcopy(original)
        request = rule["request"]
        request.pop("json")
        request["body"] = "sku={{ sku }}&" + suffix
        request["headers"] = {
            "Content-Type": f'application/x-www-form-urlencoded; note="a;b"; charset={charset}'
        }
        config["page_types"]["listing"]["follow"].insert(0, rule)
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    run_dir, _ = _crawl_config(tmp_path)
    captures = [capture for capture in _captures(run_dir) if capture["page_type"] == "product"]
    assert len(captures) == len(_records(run_dir)) == 18
    assert len({capture["request"]["fingerprint"] for capture in captures}) == 6
    for sku in _skus():
        requests = [
            capture["request"] for capture in captures if capture["variables"]["sku"] == sku
        ]
        assert len(requests) == 3
        assert len({request["fingerprint"] for request in requests}) == 1
        assert {base64.b64decode(request["body"]).decode("ascii") for request in requests} == {
            f"sku={sku}&{suffix}" for _, suffix in variants
        }


@pytest.mark.parametrize(
    ("charset", "values"),
    [("no-such-codec", ("first", "second")), ("utf-8", ("%FF", "%FE"))],
)
def test_undecodable_forms_keep_strict_original_body_fingerprints(
    tmp_path: Path, shop: _ShopServer, charset: str, values: tuple[str, str]
) -> None:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = yaml.safe_load(_site_config(base=base, settings="", session="", headers=""))
    config["records"]["product"] = {}  # one Record per request, even for a repeated sku
    original = config["page_types"]["listing"]["follow"].pop(0)
    for value in reversed(values):
        rule = copy.deepcopy(original)
        request = rule["request"]
        request.pop("json")
        request["body"] = f"sku={{{{ sku }}}}&pin={value}"
        request["headers"] = {
            "Content-Type": f"application/x-www-form-urlencoded; charset={charset}"
        }
        config["page_types"]["listing"]["follow"].insert(0, rule)
    identities = []
    for name, ignored in (("filtered", ["pin"]), ("strict", [])):
        config["replay"] = {"ignore_params": ignored}
        case_dir = tmp_path / name
        case_dir.mkdir()
        (case_dir / "e2e.yaml").write_text(yaml.safe_dump(config))
        run_dir, _ = _crawl_config(case_dir)
        captures = [capture for capture in _captures(run_dir) if capture["page_type"] == "product"]
        assert len(captures) == len(_records(run_dir)) == 12
        identities.append(
            {capture["request"]["body"]: capture["request"]["fingerprint"] for capture in captures}
        )
    assert len(set(identities[0].values())) == 12
    assert identities[0] == identities[1]


@pytest.mark.parametrize(
    ("pin", "suffix", "masked"),
    [
        (
            "abc?def",
            "/public%2Fsku/abc?def?public=visible",
            "/public%2Fsku/%5BREDACTED%5D?public=visible",
        ),
        (
            "abc#def",
            "/public%2Fsku?token=abc#def&public=visible",
            "/public%2Fsku?token=%5BREDACTED%5D&public=visible",
        ),
        (
            "abc def",
            "/public%2Fsku/abc+def?token=abc+def",
            "/public%2Fsku/abc+def?token=%5BREDACTED%5D",
        ),
    ],
)
def test_url_headers_mask_secrets_crossing_component_boundaries(
    tmp_path: Path, shop: _ShopServer, pin: str, suffix: str, masked: str
) -> None:
    shop.session_payload = {"pin": pin}
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    shop.extra_headers = [("Content-Location", base + suffix)]
    config = yaml.safe_load(_site_config(base=base, settings="", session="", headers=""))
    config["session"] = {
        "setup": [{"request": {"url": base + "/"}, "extract": {"pin": {"jsonpath": "$.pin"}}}]
    }
    config["page_types"]["listing"]["follow"][0]["request"]["headers"] = {
        "Authorization": "Bearer {{ session.pin }}",
        "Referer": base + suffix,
    }
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    run_dir, _ = _crawl_config(tmp_path)
    captures = [capture for capture in _captures(run_dir) if capture["page_type"] == "product"]
    assert len(captures) == len(_records(run_dir)) == 6
    for capture in captures:
        assert capture["request"]["headers"]["Referer"] == [base + masked]
        assert capture["response"]["headers"]["Content-Location"] == [base + masked]
    assert shop.authorizations == [f"Bearer {pin}"] * 6


def test_custom_numeric_secret_copies_are_redacted_before_json_serialization(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.session_payload = {"pin": "987.6500", "copy": "987.6500"}
    (tmp_path / "transforms.py").write_text(
        "from decimal import Decimal\ndef decimal(value, ctx):\n    return Decimal(value)\n"
    )
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = yaml.safe_load(
        _site_config(
            base=base,
            settings="",
            session="",
            headers='headers: {Authorization: "Bearer {{ session.pin }}"}',
        )
    )
    config["session"] = {
        "setup": [
            {
                "request": {"url": f"{base}/"},
                "extract": {
                    name: [{"jsonpath": f"$.{name}"}, {"fn": "transforms:decimal"}]
                    for name in ("pin", "copy")
                },
            }
        ]
    }
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    run_dir, _ = _crawl_config(tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)})
    variables = [
        capture["variables"]["session"]
        for capture in _captures(run_dir)
        if capture["page_type"] != "session_setup"
    ]
    assert variables
    assert all(value == {"pin": "[REDACTED]", "copy": "[REDACTED]"} for value in variables)
    assert len(_records(run_dir)) == 6


# Runs the CLI with IPv4/IPv6 connects logged and refused; local socketpairs still work.
NO_NETWORK = """
import errno, os, socket, sys

def guard(original, refuse):
    def connect(self, address):
        if self.family not in (socket.AF_INET, socket.AF_INET6):
            return original(self, address)
        with open(os.environ["NETWORK_LOG"], "a") as log:
            log.write(f"{address}\\n")
        return refuse()
    return connect

def refuse():
    raise ConnectionRefusedError(errno.ECONNREFUSED, "network access denied")

socket.socket.connect = guard(socket.socket.connect, refuse)
socket.socket.connect_ex = guard(socket.socket.connect_ex, lambda: errno.ECONNREFUSED)
from groceries_scraper.cli import app
sys.argv[0] = "scrape"
app()
"""


def _offline(tmp_path: Path, *args: str, exit_code: int = 0) -> tuple[Path | None, str]:
    """Runs `scrape <args>` offline; returns the new Run directory and the network log."""
    before = set((tmp_path / "runs" / "e2e").iterdir())
    log = tmp_path / "network.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", NO_NETWORK, *args],
        cwd=tmp_path,
        env={**os.environ, "NETWORK_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == exit_code, result.stderr
    new = set((tmp_path / "runs" / "e2e").iterdir()) - before
    return (new.pop() if new else None), (log.read_text() if log.exists() else "")


def _record_data(run_dir: Path) -> list[dict[str, Any]]:
    """Crawl order, and so capture numbers, can differ between Runs."""
    records = [
        {key: value for key, value in record.items() if key != "_meta"}
        for record in _records(run_dir)
    ]
    return sorted(records, key=lambda record: record["sku"])


def test_replay_reproduces_records_from_captures_without_network_access(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.rotate_every = True, 2  # Session Setup, refreshes and 419 retries
    source, _ = _run_with_log(tmp_path, shop, max_refresh=2)
    served = list(shop.paths)

    replay, network = _offline(tmp_path, "replay", str(source))

    assert replay is not None and network == ""
    assert shop.paths == served
    assert _record_data(replay) == _record_data(source)
    assert len(_records(replay)) == len(_skus())
    assert sorted((c["page_type"], c["response"]["status"]) for c in _captures(replay)) == sorted(
        (c["page_type"], c["response"]["status"]) for c in _captures(source)
    )
    manifest = _manifest(replay)
    assert manifest["replay_of"] == {"site": "e2e", "run_id": source.name}
    assert manifest["config"] == _manifest(source)["config"]
    assert manifest["missing"] == []
    assert manifest["health"]["level"] == "ok"
    # The guard itself works: a live Run under it attempts a connection.
    _, network = _offline(tmp_path, "run", "e2e.yaml", exit_code=2)
    assert network != ""


def test_replay_with_an_edited_selector_changes_records(tmp_path: Path, shop: _ShopServer) -> None:
    source, _ = _run_with_log(tmp_path, shop)
    edited = tmp_path / "edited.yaml"
    edited.write_text((tmp_path / "e2e.yaml").read_text().replace("$.name", "$.title"))

    replay, network = _offline(tmp_path, "replay", str(source), "--config", str(edited))

    assert replay is not None and network == ""
    assert {record["name"] for record in _records(source)} == {f"Product {s}" for s in _skus()}
    assert [record["name"] for record in _records(replay)] == [None] * len(_skus())
    assert _manifest(replay)["config"]["page_types"]["product"]["fields"]["name"] == {
        "<pipe>": [{"jsonpath": "$.title"}],
        "type": "string",
    }


def test_replay_records_requests_without_a_capture_as_missing(
    tmp_path: Path, shop: _ShopServer
) -> None:
    source, _ = _run_with_log(tmp_path, shop)
    edited = tmp_path / "edited.yaml"
    config = (tmp_path / "e2e.yaml").read_text()
    edited.write_text(config.replace('json: {sku: "{{ sku }}"}', 'json: {sku: "{{ sku }}x"}'))

    replay, network = _offline(
        tmp_path, "replay", str(source), "--config", str(edited), exit_code=2
    )

    assert replay is not None and network == ""
    manifest = _manifest(replay)
    assert manifest["stats"]["requests"]["missing"] == len(_skus())
    assert sorted(
        json.loads(base64.b64decode(entry["request"]["body"]))["sku"]
        for entry in manifest["missing"]
    ) == sorted(f"{sku}x" for sku in _skus())
    assert {entry["page_type"] for entry in manifest["missing"]} == {"product"}
    assert _records(replay) == []


@pytest.mark.parametrize("ignored", [True, False])
def test_replay_matches_a_rotated_csrf_body_value_only_when_ignored(
    tmp_path: Path, shop: _ShopServer, ignored: bool
) -> None:
    shop.csrf = True
    _run_with_log(tmp_path, shop, max_refresh=1)  # writes the config; this Run is discarded
    for run_dir in (tmp_path / "runs" / "e2e").iterdir():
        shutil.rmtree(run_dir)
    config = yaml.safe_load((tmp_path / "e2e.yaml").read_text())
    config["page_types"]["listing"]["follow"][0]["request"]["json"]["csrf"] = "{{ session.csrf }}"
    if ignored:
        config["replay"] = {"ignore_params": ["csrf"]}
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    source, _ = _crawl_config(tmp_path)
    [setup_body] = (source / "captures").glob("*-session_setup.body")
    setup_body.write_bytes(
        re.sub(rb'content="[0-9a-f]+"', b'content="rotated"', setup_body.read_bytes())
    )

    replay, _ = _offline(tmp_path, "replay", str(source), exit_code=0 if ignored else 2)

    assert replay is not None
    products = [c for c in _captures(replay) if c["page_type"] == "product"]
    assert {c["variables"]["session"]["csrf"] for c in products} == (
        {"rotated"} if ignored else set()
    )
    assert len(_records(replay)) == (len(_skus()) if ignored else 0)
    assert _manifest(replay)["stats"]["requests"]["missing"] == (0 if ignored else len(_skus()))


# --- Browser rendering ------------------------------------------------------


def _browser_run(tmp_path: Path, shop: _ShopServer) -> Path:
    shop.render_js = True
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = yaml.safe_load(_site_config(base=base, settings="", session="", headers=""))
    config["page_types"]["listing"]["render"] = "browser"
    (tmp_path / "e2e.yaml").write_text(yaml.safe_dump(config))
    return _crawl_config(tmp_path)[0]


def test_a_browser_rendered_page_type_extracts_from_the_rendered_dom(
    tmp_path: Path, shop: _ShopServer
) -> None:
    run_dir = _browser_run(tmp_path, shop)

    assert _record_data(run_dir) == [
        {"sku": sku, "price": float(f"{sku[1]}.{sku[2]}0"), "name": f"Product {sku}"}
        for sku in _skus()
    ]
    assert {f"/api/tiles?page={page}" for page in range(2, PAGES + 1)} <= set(shop.paths)
    captures = {c["capture_no"]: c for c in _captures(run_dir)}
    listings = [no for no, c in captures.items() if c["page_type"] == "listing"]
    assert len(listings) == PAGES
    for no in listings:
        assert captures[no]["render"] == "browser"
        [body] = (run_dir / "captures").glob(f"{no:04d}-listing.body")
        assert b'<div class="tile"' in body.read_bytes()
    assert all("render" not in c for c in captures.values() if c["page_type"] == "product")


def test_a_browser_rendered_run_replays_offline_without_a_browser(
    tmp_path: Path, shop: _ShopServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _browser_run(tmp_path, shop)
    # Launching a browser would fail: Playwright finds none here.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))

    replay, network = _offline(tmp_path, "replay", str(source))

    assert replay is not None and network == ""
    assert _record_data(replay) == _record_data(source)
    assert sorted((c["page_type"], c.get("render", "")) for c in _captures(replay)) == sorted(
        (c["page_type"], c.get("render", "")) for c in _captures(source)
    )


@pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="set TEST_DATABASE_URL to a disposable database"
)
def test_run_exports_its_records_to_a_postgres_sink(tmp_path: Path, shop: _ShopServer) -> None:
    import psycopg

    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP TABLE IF EXISTS scrape_records, scrape_runs")

    run_dir, log = _run_with_log(tmp_path, shop, "--sink", dsn)

    assert f"Exported Run {run_dir.name}" in log
    with psycopg.connect(dsn) as db:
        rows = db.execute(
            "SELECT record_key->>'sku', data->>'sku' FROM scrape_records"
            " WHERE run_id = %s AND record_type = 'product' ORDER BY 1",
            (run_dir.name,),
        ).fetchall()
    assert rows == [(sku, sku) for sku in sorted(_skus())]


# --- Locations and Session Pools -------------------------------------------------

LOCATIONS = """
locations:
  melb: {postcode: "3000"}
  syd: {postcode: "2000"}
"""


def _with_locations(config: str) -> str:
    config = config.replace("site: e2e\n", f"site: e2e\n{LOCATIONS}", 1)
    return config.replace(
        'json: {sku: "{{ sku }}"}', 'json: {sku: "{{ sku }}", postcode: "{{ location.postcode }}"}'
    )


def test_a_run_scrapes_the_location_it_is_given(tmp_path: Path, shop: _ShopServer) -> None:
    run_dir, _ = _run_with_log(tmp_path, shop, "--location", "melb", edit=_with_locations)

    assert {body["postcode"] for body in shop.bodies} == {"3000"}
    assert _manifest(run_dir)["location"] == "melb"
    assert {record["_meta"]["location"] for record in _records(run_dir)} == {"melb"}


def test_a_site_with_locations_needs_one_picked(tmp_path: Path, shop: _ShopServer) -> None:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    config = tmp_path / "e2e.yaml"
    config.write_text(_with_locations(_site_config(base=base, settings="", session="", headers="")))
    runner = CliRunner()

    missing = runner.invoke(app, ["run", str(config)])
    unknown = runner.invoke(app, ["run", str(config), "--location", "perth"])

    assert missing.exit_code == unknown.exit_code == 1
    assert "--location is required; the Site declares: melb, syd" in missing.stderr
    assert "unknown Location `perth`; the Site declares: melb, syd" in unknown.stderr
    assert shop.paths == []


def test_replay_reuses_the_source_runs_location(tmp_path: Path, shop: _ShopServer) -> None:
    source, _ = _run_with_log(tmp_path, shop, "--location", "syd", edit=_with_locations)

    replay, network = _offline(tmp_path, "replay", str(source))

    assert replay is not None and network == ""
    assert _manifest(replay)["location"] == "syd"
    assert {record["_meta"]["location"] for record in _records(replay)} == {"syd"}
    assert _record_data(replay) == _record_data(source)


def test_run_and_replay_record_the_kubernetes_job_name_when_set(
    tmp_path: Path, shop: _ShopServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRAPE_JOB_NAME", raising=False)
    local_run = _run(tmp_path, shop)
    local_replay, _ = _offline(tmp_path, "replay", str(local_run))
    monkeypatch.setenv("SCRAPE_JOB_NAME", "scrape-e2e-29301")
    job_replay, _ = _offline(tmp_path, "replay", str(local_run))
    monkeypatch.setenv("SCRAPE_JOB_NAME", "scrape-e2e-29302")
    job_run, _ = _offline(tmp_path, "run", "e2e.yaml", exit_code=2)  # offline: fails, still saved

    assert "job" not in _manifest(local_run)
    assert local_replay is not None and "job" not in _manifest(local_replay)
    assert job_replay is not None and _manifest(job_replay)["job"] == "scrape-e2e-29301"
    assert job_run is not None and _manifest(job_run)["job"] == "scrape-e2e-29302"


def _two_categories(config: str) -> str:
    dairy = "page_type: listing}]"
    return config.replace(
        dairy, dairy.replace("}]", '}, {url: "$BASE/c/bakery", page_type: listing}]'), 1
    )


def _pooled(tmp_path: Path, shop: _ShopServer, **kwargs: Any) -> tuple[Path, str]:
    base = f"http://127.0.0.1:{shop.server_address[1]}"
    return _run_with_log(
        tmp_path,
        shop,
        max_refresh=1,
        pool=2,
        edit=lambda config: _two_categories(config).replace("$BASE", base),
        **kwargs,
    )


def _sids_by_category(shop: _ShopServer) -> dict[str, set[str | None]]:
    """Product requests carry no category, so listing pages stand in for their chain."""
    sids: dict[str, set[str | None]] = {}
    for path, sid in shop.sids:
        if path.startswith("/c/"):
            sids.setdefault(urlsplit(path).path.removeprefix("/c/"), set()).add(sid)
    return sids


def test_a_session_pool_gives_each_start_request_its_own_session(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf = True

    run_dir, _ = _pooled(tmp_path, shop)

    assert sorted(r["sku"] for r in _records(run_dir)) == sorted(_skus() + _skus("bakery"))
    assert shop.paths.count("/") == 2
    sids = _sids_by_category(shop)
    assert len(sids["dairy"]) == len(sids["bakery"]) == 1
    assert sids["dairy"] != sids["bakery"]
    setup = [c for c in _captures(run_dir) if c["page_type"] == "session_setup"]
    assert sorted(c["session_no"] for c in setup) == [1, 2]
    assert "session_no" not in next(c for c in _captures(run_dir) if c["page_type"] == "listing")
    assert _manifest(run_dir)["stats"]["sessions"] == {"pool": 2, "lost": 0}


def test_a_lost_session_hands_its_start_requests_to_the_rest(
    tmp_path: Path, shop: _ShopServer
) -> None:
    shop.csrf, shop.home_failures, shop.home_failure_status = True, 1, 404

    run_dir, log = _pooled(tmp_path, shop, exit_code=1)

    assert sorted(r["sku"] for r in _records(run_dir)) == sorted(_skus() + _skus("bakery"))
    sids = _sids_by_category(shop)
    assert sids["dairy"] == sids["bakery"]  # both chains on the surviving Session
    assert _manifest(run_dir)["health"] == {
        "level": "degraded",
        "breaches": [{"check": "session/lost", "detail": "1 of 2 Sessions lost"}],
    }
    assert "Session 1 lost" in log or "Session 2 lost" in log


def test_losing_every_session_fails_the_run(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.home_status = True, 404

    run_dir, _ = _pooled(tmp_path, shop, exit_code=2)

    assert _manifest(run_dir)["health"]["breaches"][0] == {
        "check": "finish_reason",
        "detail": SESSIONS_LOST,
    }
    assert not any(path.startswith("/c/") for path in shop.paths)


def test_each_session_refreshes_on_its_own_budget(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf, shop.rotate_every = True, 6  # every token dies once, halfway through

    run_dir, log = _pooled(tmp_path, shop)

    assert sorted(r["sku"] for r in _records(run_dir)) == sorted(_skus() + _skus("bakery"))
    assert shop.paths.count("/") == 4  # each Session's Setup, then its one refresh
    assert "'session/refreshes': 2" in log


def test_a_session_lost_while_refreshing_accounts_for_its_waiting_requests(
    tmp_path: Path, shop: _ShopServer
) -> None:
    # Both Sessions refresh halfway; the second refresh's Setup fails.
    shop.csrf, shop.rotate_every, shop.home_ok_limit = True, 6, 3

    run_dir, _ = _pooled(tmp_path, shop, exit_code=1)

    stats = _manifest(run_dir)["stats"]
    assert stats["sessions"] == {"pool": 2, "lost": 1}
    assert "Session lost" in stats["requests"]["failed"]
    # Every page request ends as ok or failed: none vanish while waiting for a refresh.
    pages = [c for c in _captures(run_dir) if c["page_type"] != "session_setup"]
    refused = {c["capture_no"] for c in pages if c["response"]["status"] == 419}
    retries = [c for c in pages if c["parent_capture_no"] in refused]
    outcomes = stats["requests"]["ok"] + sum(stats["requests"]["failed"].values())
    assert outcomes == len(pages) - len(retries)


def test_replay_of_a_session_pool_reproduces_its_records(tmp_path: Path, shop: _ShopServer) -> None:
    shop.csrf = True
    source, _ = _pooled(tmp_path, shop)

    replay, network = _offline(tmp_path, "replay", str(source))

    assert replay is not None and network == ""
    assert _record_data(replay) == _record_data(source)
    assert _manifest(replay)["stats"]["requests"]["missing"] == 0
