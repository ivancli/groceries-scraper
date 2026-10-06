"""Supplied Start Requests (`supply.jsonl`) and their Start Request Outcomes (`outcomes.jsonl`)."""

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from groceries_scraper.config import Site

SUPPLY_FILE = "supply.jsonl"
OUTCOMES_FILE = "outcomes.jsonl"

StartRequestOutcome = Literal["ok", "not_found", "blocked", "skipped", "failed"]
_NOT_FOUND = (404, 410)
_BLOCKED = (403, 429)


class SupplyError(Exception):
    pass


@dataclass(frozen=True)
class SuppliedStartRequest:
    ref: str
    url: str


@dataclass(frozen=True)
class Supply:
    """A Run's Supplied Start Requests: unique refs and URLs, in supply order."""

    requests: tuple[SuppliedStartRequest, ...]

    def __iter__(self) -> Iterator[SuppliedStartRequest]:
        return iter(self.requests)

    def __len__(self) -> int:
        return len(self.requests)

    @classmethod
    def read(cls, path: Path) -> Self:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SupplyError(f"Cannot read {path}: {exc}") from None
        requests: list[SuppliedStartRequest] = []
        refs: set[str] = set()
        urls: set[str] = set()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            request = _parse(line, f"{path}:{number}")
            # One Start Request Outcome per ref, and one fetch per URL (a second would be
            # dropped as a duplicate Record Key).
            if request.ref in refs:
                raise SupplyError(f"{path}:{number}: repeated ref `{request.ref}`")
            if request.url in urls:
                raise SupplyError(f"{path}:{number}: URL supplied twice: {request.url}")
            refs.add(request.ref)
            urls.add(request.url)
            requests.append(request)
        if not requests:
            raise SupplyError(f"{path} supplies no Start Requests")
        return cls(tuple(requests))

    def check(self, site: Site) -> None:
        """A URL the Site does not accept would be parsed as the wrong page, so none is fetched."""
        if site.accepts is None:
            raise SupplyError(f"Site `{site.site}` has no Accepts Rule: it takes no supply")
        refused = [request for request in self if not site.accepts.matches(request.url)]
        if refused:
            lines = [f"  {request.ref}: {request.url}" for request in refused]
            heading = f"Supplied URLs not matching `accepts.url` ({site.accepts.url}):"
            raise SupplyError("\n".join([heading, *lines]))

    def write(self, path: Path) -> None:
        path.write_text(self.jsonl(), encoding="utf-8")

    def jsonl(self) -> str:
        return _jsonl({"ref": r.ref, "url": r.url} for r in self)


def _parse(line: str, where: str) -> SuppliedStartRequest:
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise SupplyError(f"{where}: not JSON: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"ref", "url"}:
        raise SupplyError(f'{where}: expected {{"ref": …, "url": …}}')
    ref, url = data["ref"], data["url"]
    if not (isinstance(ref, str) and ref):
        raise SupplyError(f"{where}: `ref` must be a non-empty string")
    # Python's `$` also matches before a trailing newline; JavaScript's doesn't.
    if not (isinstance(url, str) and url and not any(char.isspace() for char in url)):
        raise SupplyError(f"{where}: `url` must be non-empty and free of whitespace")
    return SuppliedStartRequest(ref, url)


def write_jsonl(path: Path, lines: Iterable[dict[str, Any]]) -> None:
    path.write_text(_jsonl(lines), encoding="utf-8")


def _jsonl(lines: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)


class SupplyOutcomes:
    """A ref with a Record is `ok`; otherwise its last ending counts, and no ending is `skipped`.

    Requests without a ref (an unsupplied Run's) are ignored.
    """

    def __init__(self, supply: Supply) -> None:
        self._supply = supply
        self._records: Counter[str] = Counter()
        self._ended: dict[str, tuple[StartRequestOutcome, str | None]] = {}

    def add_record(self, ref: str | None) -> None:
        if ref is not None:
            self._records[ref] += 1

    def http_error(self, ref: str | None, status: int) -> None:
        if status in _NOT_FOUND:
            self._end(ref, "not_found")
        elif status in _BLOCKED:
            self._end(ref, "blocked")
        else:
            self._end(ref, "failed", f"HTTP {status}")

    def skipped(self, ref: str | None) -> None:
        self._end(ref, "skipped")

    def failed(self, ref: str | None, error: str) -> None:
        self._end(ref, "failed", error)

    def _end(self, ref: str | None, outcome: StartRequestOutcome, error: str | None = None) -> None:
        if ref is not None:
            self._ended[ref] = (outcome, error)

    def write(self, path: Path) -> None:
        write_jsonl(path, self._lines())

    def _lines(self) -> Iterator[dict[str, Any]]:
        for request in self._supply:
            outcome, error = (
                ("ok", None)
                if self._records[request.ref]
                else self._ended.get(request.ref, ("skipped", None))
            )
            line: dict[str, Any] = {"ref": request.ref, "url": request.url, "outcome": outcome}
            if error is not None:
                line["error"] = error
            yield line
