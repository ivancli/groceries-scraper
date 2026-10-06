"""Supplied Start Requests (`supply.jsonl`) and their Start Request Outcomes (`outcomes.jsonl`)."""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from groceries_scraper.config import Site

SUPPLY_FILE = "supply.jsonl"
OUTCOMES_FILE = "outcomes.jsonl"

Outcome = Literal["ok", "not_found", "blocked", "skipped", "failed"]
_NOT_FOUND = (404, 410)
_BLOCKED = (403, 429)


class SupplyError(Exception):
    pass


@dataclass(frozen=True)
class SuppliedStartRequest:
    ref: str
    url: str


def read_supply(path: Path) -> list[SuppliedStartRequest]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SupplyError(f"Cannot read {path}: {exc}") from None
    supply: list[SuppliedStartRequest] = []
    refs: set[str] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise SupplyError(f"{where}: not JSON: {exc}") from None
        if not isinstance(data, dict) or set(data) != {"ref", "url"}:
            raise SupplyError(f'{where}: expected {{"ref": …, "url": …}}')
        ref, url = data["ref"], data["url"]
        if not (isinstance(ref, str) and ref and isinstance(url, str) and url):
            raise SupplyError(f"{where}: `ref` and `url` must be non-empty strings")
        # One Start Request Outcome per ref: a repeat would be ambiguous.
        if ref in refs:
            raise SupplyError(f"{where}: repeated ref `{ref}`")
        refs.add(ref)
        supply.append(SuppliedStartRequest(ref, url))
    if not supply:
        raise SupplyError(f"{path} supplies no Start Requests")
    return supply


def check_supply(site: Site, supply: list[SuppliedStartRequest]) -> None:
    """A URL the Site does not accept would be parsed as the wrong page, so none is fetched."""
    if site.accepts is None:
        raise SupplyError(f"Site `{site.site}` has no Accepts Rule: it takes no supply")
    refused = [request for request in supply if not site.accepts.matches(request.url)]
    if refused:
        lines = [f"  {request.ref}: {request.url}" for request in refused]
        raise SupplyError(
            "\n".join([f"Supplied URLs not matching `accepts.url` ({site.accepts.url}):", *lines])
        )


def write_supply(path: Path, supply: list[SuppliedStartRequest]) -> None:
    lines = [json.dumps({"ref": r.ref, "url": r.url}, ensure_ascii=False) for r in supply]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


class SupplyOutcomes:
    """A ref with a Record is `ok`; otherwise its last ending counts, and no ending is `skipped`."""

    def __init__(self, supply: list[SuppliedStartRequest]) -> None:
        self._supply = supply
        self._records: Counter[str] = Counter()
        self._ended: dict[str, tuple[Outcome, str | None]] = {}

    def add_record(self, ref: str) -> None:
        self._records[ref] += 1

    def http_error(self, ref: str, status: int) -> None:
        if status in _NOT_FOUND:
            self._ended[ref] = ("not_found", None)
        elif status in _BLOCKED:
            self._ended[ref] = ("blocked", None)
        else:
            self.failed(ref, f"HTTP {status}")

    def skipped(self, ref: str) -> None:
        self._ended[ref] = ("skipped", None)

    def failed(self, ref: str, error: str) -> None:
        self._ended[ref] = ("failed", error)

    def to_json(self) -> list[dict[str, Any]]:
        lines = []
        for request in self._supply:
            outcome, error = (
                ("ok", None)
                if self._records[request.ref]
                else self._ended.get(request.ref, ("skipped", None))
            )
            line: dict[str, Any] = {"ref": request.ref, "url": request.url, "outcome": outcome}
            if error is not None:
                line["error"] = error
            lines.append(line)
        return lines
