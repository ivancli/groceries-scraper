"""Overlapping price Runs against a fake retailer, coordinated through disposable Postgres."""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from groceries_scraper.coordination import CoordinatorAdmin

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROLE = "scrape_coordinator_test"
PASSWORD = "coordinator-test"
INTERVAL = 0.3
# Grant-to-arrival latency differs a little between requests.
JITTER = 0.05
SCRAPE = str(Path(sys.executable).with_name("scrape"))

PRODUCT = """<html><head><link rel="canonical" href="{url}"></head>
<body><h1>Product {n}</h1><span class="price">{n}.50</span></body></html>"""
# The script, its fetch and the image are retailer requests a browser makes on its own.
BROWSER_PAGE = """<html><head><link rel="canonical" href="{url}">
<script src="/app.js"></script></head><body><h1>Rendered</h1>
<span class="price"></span><img src="/logo.png"></body></html>"""
APP_JS = """fetch('/api/price').then(r => r.text()).then(price => {
  document.querySelector('.price').textContent = price;
});"""


@dataclass
class Arrival:
    path: str
    at: float
    in_flight: int
    permits: int


@dataclass
class Retailer:
    port: int
    arrivals: list[Arrival] = field(default_factory=list)
    in_flight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def _permits_held() -> int:
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        row = connection.execute("SELECT count(*) FROM coordination.permits").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def retailer() -> Iterator[Retailer]:
    state: Retailer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with state.lock:
                state.in_flight += 1
                arrival = Arrival(self.path, time.monotonic(), state.in_flight, _permits_held())
                state.arrivals.append(arrival)
            try:
                time.sleep(0.15)  # long enough for Runs to overlap
                self._respond()
            finally:
                with state.lock:
                    state.in_flight -= 1

        def _respond(self) -> None:
            path = self.path
            if path == "/robots.txt":
                self._send("text/plain", "User-agent: *\nAllow: /\n")
            elif path in ("/moved", "/old-page"):
                self.send_response(302)
                self.send_header("Location", "/p/99" if path == "/moved" else "/page")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path.startswith("/p/"):
                n = path.removeprefix("/p/")
                self._send("text/html", PRODUCT.format(url=state.url(path), n=n))
            elif path == "/page":
                self._send("text/html", BROWSER_PAGE.format(url=state.url("/page")))
            elif path == "/app.js":
                self._send("application/javascript", APP_JS)
            elif path == "/api/price":
                self._send("text/plain", "7.25")
            elif path == "/logo.png":
                self._send("image/png", "")
            else:
                self.send_error(404)

        def _send(self, content_type: str, body: str) -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = Retailer(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def coordinator_env() -> dict[str, str]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS coordination CASCADE")
    CoordinatorAdmin(DSN).prepare(ROLE, PASSWORD)
    return {
        "SCRAPE_COORDINATOR": make_conninfo(DSN, user=ROLE, password=None),
        "SCRAPE_COORDINATOR_PASSWORD": PASSWORD,
    }


def _site(tmp_path: Path, retailer: Retailer, name: str, paths: list[str], render: str) -> Path:
    starts = "\n".join(f"  - {{url: '{retailer.url(p)}', page_type: product}}" for p in paths)
    config = tmp_path / f"{name}.yaml"
    config.write_text(f"""
site: {name}
settings:
  download_delay: {INTERVAL}
  concurrent_requests_per_domain: 2
  obey_robots: true
  record_level: "off"
schedule: {{every: 30m}}
accepts:
  page_type: product
  retailer: {{key: shop, name: Shop, hosts: ["127.0.0.1"]}}
  url: '^http://127\\.0\\.0\\.1:[0-9]+/'
  examples: ['{retailer.url("/p/1")}']
records:
  product: {{key: [url]}}
start:
{starts}
page_types:
  product:
    record: product
    render: {render}
    fields:
      url: {{xpath: "//link[@rel='canonical']/@href", type: string, required: true}}
      name: {{xpath: "//h1/text()", type: string, required: true}}
      price: {{xpath: "//span[@class='price']/text()", type: number, required: true}}
""")
    return config


def _start_run(tmp_path: Path, config: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [SCRAPE, "run", str(config)],
        cwd=tmp_path,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_together(tmp_path: Path, configs: list[Path], env: dict[str, str]) -> None:
    processes = [_start_run(tmp_path, config, env) for config in configs]
    for process in processes:
        output, _ = process.communicate(timeout=120)
        assert process.returncode == 0, output


def _gaps(arrivals: list[Arrival]) -> list[float]:
    times = sorted(arrival.at for arrival in arrivals)
    return [later - earlier for earlier, later in zip(times, times[1:], strict=False)]


def _two_overlapping_runs(tmp_path: Path, retailer: Retailer) -> list[Path]:
    return [
        _site(tmp_path, retailer, "shop_a", [f"/p/{n}" for n in range(1, 5)], "http"),
        _site(tmp_path, retailer, "shop_b", ["/moved", *(f"/p/{n}" for n in range(5, 8))], "http"),
    ]


def test_overlapping_price_runs_share_the_retailers_pace_and_concurrency(
    tmp_path: Path, retailer: Retailer, coordinator_env: dict[str, str]
) -> None:
    _run_together(tmp_path, _two_overlapping_runs(tmp_path, retailer), coordinator_env)

    paths = [arrival.path for arrival in retailer.arrivals]
    # Both Runs' robots.txt and the redirect hop are retailer requests too.
    assert paths.count("/robots.txt") == 2
    assert paths.count("/p/99") == 1
    assert len(paths) == 2 + 8 + 1
    assert min(_gaps(retailer.arrivals)) >= INTERVAL - JITTER
    assert max(arrival.in_flight for arrival in retailer.arrivals) <= 2
    # Every request reached the retailer holding a permit, and none outlived its Run.
    assert all(arrival.permits >= 1 for arrival in retailer.arrivals)
    assert _permits_held() == 0


def test_without_a_coordinator_the_same_runs_exceed_the_shared_pace(
    tmp_path: Path, retailer: Retailer
) -> None:
    _run_together(tmp_path, _two_overlapping_runs(tmp_path, retailer), {})

    # Each Run keeps only its own pace: the shared bound above is the coordinator's.
    assert min(_gaps(retailer.arrivals)) < INTERVAL - JITTER


def test_a_browser_runs_navigation_redirects_and_page_requests_all_take_permits(
    tmp_path: Path, retailer: Retailer, coordinator_env: dict[str, str]
) -> None:
    config = _site(tmp_path, retailer, "shop_browser", ["/old-page"], "browser")

    _run_together(tmp_path, [config], coordinator_env)

    by_path = {arrival.path: arrival for arrival in retailer.arrivals}
    assert set(by_path) >= {"/old-page", "/page", "/app.js", "/api/price", "/logo.png"}
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        row = connection.execute("SELECT last_value FROM coordination.permits_id_seq").fetchone()
    # One permit per request that reached the retailer, the browser's own included.
    assert row is not None and row[0] == len(retailer.arrivals)
    # Only the redirect hop is accounted for after it is sent.
    routed = [arrival for arrival in retailer.arrivals if arrival.path != "/page"]
    assert all(arrival.permits >= 1 for arrival in routed)
    assert by_path["/old-page"].at - by_path["/robots.txt"].at >= INTERVAL - JITTER
    assert _permits_held() == 0


def test_a_price_run_sends_nothing_while_the_coordinator_is_unreachable(
    tmp_path: Path, retailer: Retailer
) -> None:
    config = _site(tmp_path, retailer, "shop_a", ["/p/1", "/p/2"], "http")
    env = {"SCRAPE_COORDINATOR": "postgresql://nobody@127.0.0.1:1/none"}

    process = _start_run(tmp_path, config, env)
    output, _ = process.communicate(timeout=120)

    assert process.returncode != 0, output
    assert retailer.arrivals == []
