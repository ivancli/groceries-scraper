"""Search adapters: a Site without Accepts Rule or Schedule, plus how one query starts and ends.

Loaded apart from `sites/`, so price configuration loading and the Site Catalogue never see
them, and their Records never meet the Price Record contract or a Sink.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from groceries_scraper.config import ConfigError, Findings, Site, checked_site
from groceries_scraper.config.loader import DEFAULTS_PATH
from groceries_scraper.config.models import DEFAULT_LOCATION, RetailerRef

QUERY = "{query}"
MAX_QUERY = 200  # code points, as the contract's request bound


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Verify(_Model):
    """XPath tests on the search page's response that decide what an outcome is."""

    # Matches only a genuine search response; anything else is a failure.
    page: str
    # Matches only a response that positively shows no results.
    empty: str
    # A challenge or refusal page; with these statuses, the outcome is blocked.
    blocked: str | None = None
    blocked_statuses: list[int] = Field(default_factory=lambda: [401, 403, 429])


class SearchSpec(_Model):
    adapter_version: Annotated[str, Field(min_length=1, max_length=64)]
    retailer: RetailerRef
    enabled: bool = False
    url: str
    page_type: str
    record: str
    max_results: Annotated[int, Field(ge=1, le=10)] = 10
    verify: Verify

    @field_validator("url")
    @classmethod
    def _fixed_origin(cls, url: str) -> str:
        # The query only fills a slot in a reviewed URL; it can't pick the origin or path.
        one, other = urlsplit(url.replace(QUERY, "a")), urlsplit(url.replace(QUERY, "b"))
        if url.count(QUERY) != 1 or one.netloc != other.netloc:
            raise ValueError(f"`url` needs exactly one `{QUERY}`, outside the host")
        if one.scheme not in ("http", "https"):
            raise ValueError("`url` must be http(s)")
        return url


@dataclass(frozen=True)
class SearchAdapter:
    spec: SearchSpec
    site: Site
    source: Path

    @property
    def retailer(self) -> str:
        return self.spec.retailer.key

    @property
    def hosts(self) -> tuple[str, ...]:
        """Whose requests take Retailer Permits: declared `hosts`, or the search URL's host."""
        if self.spec.retailer.hosts:
            return tuple(self.spec.retailer.hosts)
        return (urlsplit(self.spec.url.replace(QUERY, "q")).hostname or "",)

    @property
    def locations(self) -> tuple[str, ...]:
        """Locations a request may name; the Site's own, or the default one."""
        return tuple(self.site.locations) or (DEFAULT_LOCATION,)

    def site_for(self, query: str) -> Site:
        """The Site for one query: its only Start Request is the reviewed search URL."""
        url = self.spec.url.replace(QUERY, quote(query, safe=""))
        start = {"url": url, "page_type": self.spec.page_type}
        return self.site.model_copy(
            update={"start": [type(self.site.start[0]).model_validate(start)]}
        )


def valid_query(query: object) -> bool:
    return isinstance(query, str) and 0 < len(query) <= MAX_QUERY and bool(query.strip())


def load_adapter(path: Path, defaults_path: Path = DEFAULTS_PATH) -> SearchAdapter:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, Mapping) or not isinstance(data.get("search"), Mapping):
        raise ConfigError(str(path), Findings(["a search adapter needs a `search:` mapping"]))
    data = dict(data)
    raw_spec = data.pop("search")
    try:
        spec = SearchSpec.model_validate(raw_spec)
    except ValidationError as exc:
        errors = [f"search.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(str(path), Findings(errors)) from None
    findings = []
    if "accepts" in data or "schedule" in data:
        findings.append("a search adapter has no `accepts` or `schedule`: it never checks prices")
    if "start" in data:
        findings.append("a search adapter has no `start`: each query makes its own")
    if findings:
        raise ConfigError(str(path), Findings(findings))
    data.setdefault("site", f"{spec.retailer.key}_search")
    data["start"] = [{"url": spec.url.replace(QUERY, "placeholder"), "page_type": spec.page_type}]
    defaults: Any = yaml.safe_load(defaults_path.read_text())
    site, _ = checked_site(data, defaults, str(path))
    page_type = site.page_types.get(spec.page_type)
    if page_type is None or page_type.record != spec.record:
        message = f"page type `{spec.page_type}` must emit the `{spec.record}` Record Type"
        raise ConfigError(str(path), Findings([message]))
    return SearchAdapter(spec, site, path)


def load_adapters(directory: Path) -> dict[str, SearchAdapter]:
    """Keyed by retailer; one adapter each, so a request can't choose between them."""
    adapters: dict[str, SearchAdapter] = {}
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        adapter = load_adapter(path)
        if adapter.retailer in adapters:
            message = f"retailer `{adapter.retailer}` already has a search adapter"
            raise ConfigError(str(path), Findings([message]))
        adapters[adapter.retailer] = adapter
    return adapters
