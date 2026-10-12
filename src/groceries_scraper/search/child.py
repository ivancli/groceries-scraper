"""`python -m groceries_scraper.search.child FD`: one search, crawled in its own process.

The supervisor starts it in an empty scratch directory with an allowlisted environment and
talks to it over the socket FD: the job in, Retailer Permits back and forth, one result out.
"""

import asyncio
import json
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from groceries_scraper.adapter.crawl import crawl
from groceries_scraper.adapter.traffic import RetailerTraffic
from groceries_scraper.config import Site
from groceries_scraper.coordination import CoordinatorUnavailable, Permit
from groceries_scraper.engine.page import parse_scope
from groceries_scraper.run.directory import SavedRun, create_run
from groceries_scraper.run.inspection import decompress_body, response_header
from groceries_scraper.run.keys import DUPLICATE_KEY
from groceries_scraper.search.adapters import SearchAdapter, load_adapter

# Reviewed for search: results come from the document and its scripts' data, never from these.
BLOCKED_RESOURCES = frozenset({"image", "media", "font"})


class Channel:
    """The supervisor socket; one exchange at a time. Settings deep-copy it as itself."""

    def __init__(self, fd: int) -> None:
        self._socket = socket.socket(fileno=fd)
        self._reader = self._socket.makefile("r", encoding="utf-8")
        self._lock = threading.Lock()

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def send(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._socket.sendall((json.dumps(message) + "\n").encode())

    def receive(self) -> dict[str, Any]:
        line = self._reader.readline()
        if not line:
            raise CoordinatorUnavailable("the supervisor closed the channel")
        message: dict[str, Any] = json.loads(line)
        return message

    def ask(self, message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._socket.sendall((json.dumps(message) + "\n").encode())
            return self.receive()


class SupervisorPermits:
    """Permits granted to the admitted execution; the child never holds a credential."""

    def __init__(self, channel: Channel, retailer: str) -> None:
        self.channel = channel
        self.retailer = retailer
        self._ids = iter(range(1, 1_000_000))

    async def acquire(self, retailer: str, *, paced: bool, charge: bool) -> Permit:
        request = {"type": "permit", "id": next(self._ids), "paced": paced, "charge": charge}
        reply = await asyncio.to_thread(self.channel.ask, request)
        if not isinstance(granted := reply.get("granted"), int):
            raise CoordinatorUnavailable("the supervisor refused a permit")
        return Permit(granted, retailer, "", datetime.now(UTC))

    async def release(self, permit: Permit) -> None:
        await asyncio.to_thread(self.channel.send, {"type": "release", "permit": permit.id})


def search_settings(budget: float) -> dict[str, Any]:
    """One bounded attempt: no retries, and no wait longer than the budget."""
    return {
        "RETRY_ENABLED": False,
        "DOWNLOAD_TIMEOUT": budget,
        "CLOSESPIDER_TIMEOUT": budget,
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": budget * 1000,
        "LOG_LEVEL": "WARNING",
    }


def search_site(adapter: SearchAdapter, query: str) -> Site:
    site = adapter.site_for(query)
    # Captures are needed to verify the response; a Session refresh would be a retry.
    settings = site.settings.model_copy(update={"record_level": "all"})
    session = site.session and site.session.model_copy(update={"max_refresh": 0})
    return site.model_copy(update={"settings": settings, "session": session})


def classify(adapter: SearchAdapter, run: SavedRun, corrupt: int) -> dict[str, Any]:
    """Only a verified search response is a result; zero Records alone never is."""
    verify = adapter.spec.verify
    captures = [
        (path, meta)
        for path in sorted(run.path.glob("captures/*.meta.json"))
        if (meta := json.loads(path.read_text()))["page_type"] == adapter.spec.page_type
    ]
    if not captures:
        return _result("failed", error="no_response")
    path, meta = captures[-1]  # after any redirects
    body_path = path.with_name(path.name.removesuffix(".meta.json") + ".body")
    try:
        body = decompress_body(body_path.read_bytes(), response_header(meta, "Content-Encoding"))
    except (OSError, ValueError):
        return _result("failed", error="undecodable_response")
    status = meta["response"]["status"]
    page = parse_scope("html", body, response_header(meta, "Content-Type"))
    if status in verify.blocked_statuses or (verify.blocked and page.xpath(verify.blocked)):
        return _result("blocked")
    if status >= 400:
        return _result("failed", error=f"http_{status}")
    if not page.xpath(verify.page):
        return _result("failed", error="unrecognized_page")
    if corrupt:
        return _result("failed", error="corrupt_result")
    results: list[dict[str, Any]] = []
    for record in run.records(adapter.spec.record):
        record.pop("_meta", None)
        if all(record.get("url") != seen.get("url") for seen in results):
            results.append(record)
    if not results and not page.xpath(verify.empty):
        return _result("failed", error="unverified_empty")
    return _result("ok", results[: adapter.spec.max_results])


def _result(
    status: str, results: list[dict[str, Any]] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {"type": "result", "status": status, "results": results or [], "error": error}


def main(argv: list[str]) -> int:
    channel = Channel(int(argv[1]))
    job = channel.receive()
    adapter = load_adapter(Path(job["adapter"]))
    site = search_site(adapter, job["query"])
    location = site.resolve_location(job["location"])
    run = create_run(Path("runs"), site.site, location=location)
    permits = SupervisorPermits(channel, adapter.retailer)
    traffic = RetailerTraffic(adapter.retailer, adapter.hosts, permits, BLOCKED_RESOURCES)
    outcome = crawl(site, run, traffic=traffic, overrides=search_settings(float(job["budget"])))
    # A repeated product is the retailer's duplicate tile, not a broken one.
    drops = outcome.stats.drops_by_reason
    corrupt = sum(count for kind, count in drops.items() if kind != DUPLICATE_KEY)
    channel.send(classify(adapter, SavedRun.load(run.path), corrupt))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
