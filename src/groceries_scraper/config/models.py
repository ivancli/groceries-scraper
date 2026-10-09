"""Pydantic models for a Site config (see docs/design.md)."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PlainSerializer,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

# Aliases/tags in angle brackets are internal: error paths drop them to match the YAML.
PIPE_KEY = "<pipe>"
STEP_TAG = "<step>"
STEPS_TAG = "<steps>"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _config_error(message: str) -> PydanticCustomError:
    return PydanticCustomError("config", message)


# --- Pipes ------------------------------------------------------------------

STEP_KINDS = (
    "css",
    "xpath",
    "jsonpath",
    "parse",
    "var",
    "regex",
    "replace",
    "strip",
    "split",
    "join",
    "lower",
    "upper",
    "urljoin",
    "template",
    "fn",
)
NO_ARG_KINDS = ("strip", "lower", "upper", "urljoin")


ScopeKind = Literal["html", "json"]
RecordLevel = Literal["all", "errors", "off"]


class Step(_Model):
    css: str | None = None
    xpath: str | None = None
    jsonpath: str | None = None
    parse: ScopeKind | None = None
    var: str | None = None
    regex: str | None = None
    replace: tuple[str, str] | None = None
    strip: Literal[True] | None = None
    split: str | None = None
    join: str | None = None
    lower: Literal[True] | None = None
    upper: Literal[True] | None = None
    urljoin: Literal[True] | None = None
    template: str | None = None
    fn: Annotated[str, Field(pattern=r"^[\w.]+:[\w.]+$")] | None = None
    absolute: bool = False

    @model_validator(mode="before")
    @classmethod
    def _bare_kind(cls, data: Any) -> Any:
        # `- strip` in a Pipe list means `{strip: true}`.
        if isinstance(data, str) and data in NO_ARG_KINDS:
            return {data: True}
        return data

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> Step:
        kinds = self._kinds()
        if len(kinds) != 1:
            raise _config_error(
                f"a Step needs exactly one of: {', '.join(STEP_KINDS)}; "
                f"found: {', '.join(kinds) or 'none'}"
            )
        return self

    def _kinds(self) -> list[str]:
        return [name for name in STEP_KINDS if getattr(self, name) is not None]

    @property
    def kind(self) -> str:
        return self._kinds()[0]


def resolve_fn(ref: str) -> Callable[..., Any]:
    """The callable an `fn:` Step names; shared so validation fails exactly when a Run would."""
    module, _, name = ref.partition(":")
    fn = getattr(importlib.import_module(module), name)
    if not callable(fn):
        raise TypeError(f"`{ref}` is not callable")
    return fn  # type: ignore[no-any-return]


def _as_list(value: Step | list[Step]) -> list[Step]:
    return value if isinstance(value, list) else [value]


# A single Step mapping is shorthand for a one-step Pipe; tagging the two forms keeps the
# list index out of error paths for the shorthand.
if TYPE_CHECKING:
    Pipe = list[Step]
else:
    Pipe = Annotated[
        Annotated[Step, Tag(STEP_TAG)] | Annotated[list[Step], Field(min_length=1), Tag(STEPS_TAG)],
        Discriminator(lambda value: STEPS_TAG if isinstance(value, list) else STEP_TAG),
        AfterValidator(_as_list),
    ]


# --- Fields -----------------------------------------------------------------

FIELD_KEYS = {"type", "required", "default", "fields", "items", "each"}
FieldTypeName = Literal["string", "number", "integer", "boolean", "object", "array"]


def _check_scalar_items(items: FieldSpec | ContractField) -> None:
    # The engine only coerces each match; anything else in `items` would be ignored.
    if items.type in ("object", "array") or items.model_fields_set - {"type"}:
        raise _config_error("an array Field's `items` only takes a scalar `type`")


class FieldSpec(_Model):
    type: FieldTypeName | None = None
    required: bool = False
    default: Any = None
    pipe: Pipe = Field(default_factory=list, alias="<pipe>")  # mypy needs a literal; == PIPE_KEY
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    items: FieldSpec | None = None
    each: Pipe = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _inline_pipe(cls, data: Any) -> Any:
        # Pipe keys sit inline with Field keys (`{css: ..., type: string}`), the whole
        # Field is a Pipe list, or a typed Field needing several steps names them in `pipe`.
        if isinstance(data, list):
            return {PIPE_KEY: data}
        if not isinstance(data, dict) or PIPE_KEY in data:
            return data
        own = {k: v for k, v in data.items() if k in FIELD_KEYS}
        step = {k: v for k, v in data.items() if k not in FIELD_KEYS | {"pipe"}}
        if "pipe" in data:
            if step:
                raise _config_error("a Field takes `pipe` or inline Pipe steps, not both")
            return {**own, PIPE_KEY: data["pipe"]}
        return {**own, PIPE_KEY: step} if step else own

    @model_validator(mode="after")
    def _shape_matches_type(self) -> FieldSpec:
        if self.type == "array":
            has_items, has_each = self.items is not None, bool(self.each)
            if has_items == has_each:
                raise _config_error("an array Field needs exactly one of `items` or `each`")
            if has_each and not self.fields:
                raise _config_error("an array Field with `each` needs `fields`")
            if has_items and self.fields:
                raise _config_error("an array Field with `items` cannot have `fields`")
            if self.items is not None:
                _check_scalar_items(self.items)
        elif self.type == "object":
            if not self.fields:
                raise _config_error("an object Field needs `fields`")
            if self.items is not None or self.each:
                raise _config_error("`items` and `each` are only allowed on array Fields")
        elif self.fields or self.items is not None or self.each:
            raise _config_error("`fields`, `items` and `each` need type object or array")
        return self


# --- Requests and crawl structure -------------------------------------------


class RequestTemplate(_Model):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str | None = None  # None: the URL the Follow Rule selected
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = Field(default=None, alias="json")
    form: dict[str, str] | None = None
    body: str | None = None

    @model_validator(mode="after")
    def _one_body(self) -> RequestTemplate:
        bodies = [self.json_body, self.form, self.body]
        if sum(body is not None for body in bodies) > 1:
            raise _config_error("a Request Template takes one of `json`, `form` or `body`")
        return self


# Always in a template's scope, so an `as:` or `pass:` name using one would be ignored.
TEMPLATE_NAMES = ("value", "session", "location", "env")


class StartRequest(_Model):
    url: str
    page_type: str


class Loop(_Model):
    each: Pipe


class FollowRule(_Model):
    select: Pipe
    scope: Literal["page", "each"] = "page"
    page_type: str
    as_: str | None = Field(default=None, alias="as")
    pass_: dict[str, Pipe] = Field(default_factory=dict, alias="pass")
    request: RequestTemplate | None = None

    @property
    def template_names(self) -> frozenset[str]:
        """Names this rule adds to its Request Template's scope."""
        return frozenset([*self.pass_, *([self.as_] if self.as_ else [])])

    @model_validator(mode="after")
    def _names_not_reserved(self) -> FollowRule:
        if reserved := sorted(self.template_names & set(TEMPLATE_NAMES)):
            raise _config_error(
                f"`as` and `pass` cannot use reserved template names: {', '.join(reserved)}"
            )
        return self


class PageType(_Model):
    response: ScopeKind = "html"
    render: Literal["http", "browser"] = "http"
    record: str | None = None
    items: Loop | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    follow: list[FollowRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _each_scope_has_loop(self) -> PageType:
        if self.items is None and any(rule.scope == "each" for rule in self.follow):
            raise _config_error("a Follow Rule with `scope: each` needs a page-level `items` Loop")
        return self

    @property
    def renders_in_browser(self) -> bool:
        return self.render == "browser"

    @model_validator(mode="after")
    def _browser_renders_html(self) -> PageType:
        # The browser's output is its rendered DOM, never the raw JSON.
        if self.renders_in_browser and self.response != "html":
            raise _config_error("`render: browser` needs `response: html`")
        return self


# --- Site-level sections ----------------------------------------------------


class Settings(_Model):
    """Allowlist of runtime settings; values come from defaults.yaml merged with the Site."""

    download_delay: Annotated[float, Field(ge=0)]
    concurrent_requests_per_domain: Annotated[int, Field(ge=1)]
    obey_robots: bool
    record_level: RecordLevel
    redact_headers: list[str] = Field(default_factory=list)
    # For Sites whose bot protection challenges Scrapy's own User-Agent.
    user_agent: str | None = None


class SetupStep(_Model):
    request: RequestTemplate
    extract: dict[str, Pipe] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_url(self) -> SetupStep:
        if self.request.url is None:
            raise _config_error("a Session Setup request needs a `url`")
        return self


class Session(_Model):
    pool: Annotated[int, Field(ge=1)] = 1
    setup: list[SetupStep]
    refresh_on: list[int] = Field(default_factory=list)
    max_refresh: Annotated[int, Field(ge=0)] = 1


DEFAULT_LOCATION = "default"  # the implicit Location of a Site that declares none
_LOCATION_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _check_locations(locations: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # Names become S3 path segments.
    if bad := [name for name in locations if not _LOCATION_NAME.fullmatch(name)]:
        names = ", ".join(f"`{name}`" for name in bad)
        raise _config_error(f"Location names use letters, digits, `_` and `-`: {names}")
    # `label` is the tracker's display text for the Location.
    for name, variables in locations.items():
        if not isinstance(variables.get("label", ""), str):
            raise _config_error(f"Location `{name}` has a non-string `label`")
    return locations


class Replay(_Model):
    ignore_params: list[str] = Field(default_factory=list)


class ContractField(_Model):
    """A Field in a Record Contract; an array of objects takes `fields` (no `each`)."""

    type: FieldTypeName
    required: bool = False
    fields: dict[str, ContractField] = Field(default_factory=dict)
    items: ContractField | None = None

    @model_validator(mode="after")
    def _shape_matches_type(self) -> ContractField:
        if self.type == "array":
            if (self.items is None) == (not self.fields):
                raise _config_error("an array Field needs exactly one of `items` or `fields`")
            if self.items is not None:
                _check_scalar_items(self.items)
        elif self.type == "object":
            if not self.fields:
                raise _config_error("an object Field needs `fields`")
            if self.items is not None:
                raise _config_error("`items` is only allowed on array Fields")
        elif self.fields or self.items is not None:
            raise _config_error("`fields` and `items` need type object or array")
        return self


class RecordType(_Model):
    key: list[str] = Field(default_factory=list)  # a Record Type has at most one Record Key
    # None: no Record Contract.
    fields: Annotated[dict[str, ContractField], Field(min_length=1)] | None = None


Ratio = Annotated[float, Field(ge=0, le=1)]


class Health(_Model):
    min_records: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    max_dropped_ratio: Ratio | None = None
    max_null_ratio: dict[str, Ratio] = Field(default_factory=dict)
    max_http_error_ratio: Ratio | None = None


def _shared_regex(pattern: str) -> str:
    """The tracker matches the same pattern in JavaScript, so only the common subset is allowed."""
    if not pattern.startswith("^"):
        raise _config_error("the pattern must start with `^`")
    if construct := _outside_shared_subset(pattern):
        raise _config_error(
            f"`{construct}` is outside the Python/JavaScript regex subset: use only "
            "`(?:…)`, `(?=…)` and `(?!…)` groups, no anchors besides `^` and `$`, "
            "and no `{,n}` or possessive quantifiers"
        )
    try:
        re.compile(pattern, re.ASCII)
    except re.error as exc:
        raise _config_error(f"invalid pattern: {exc}") from None
    return pattern


def _outside_shared_subset(pattern: str) -> str | None:
    i, in_class = 0, False
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            escaped = pattern[i : i + 2]
            if not in_class and escaped in ("\\A", "\\Z", "\\z"):
                return escaped
            i += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif pattern.startswith("(?", i) and pattern[i + 2 : i + 3] not in (":", "=", "!"):
            return pattern[i : i + 4]
        elif pattern.startswith("{,", i):
            return "{,"
        elif char in "*+?}" and pattern[i + 1 : i + 2] == "+":
            return pattern[i : i + 2]
        i += 1
    return None


class AcceptsRule(_Model):
    """Which URLs the Site takes as Supplied Start Requests, and the Page Type they start at."""

    page_type: str
    url: Annotated[str, AfterValidator(_shared_regex)]
    examples: Annotated[list[str], Field(min_length=1)]

    def matches(self, url: str) -> bool:
        # ASCII classes, like JavaScript's without the `u` flag.
        return re.search(self.url, url, re.ASCII) is not None


def _price_record() -> dict[str, ContractField]:
    required: dict[str, FieldTypeName] = {"url": "string", "name": "string", "price": "number"}
    optional: dict[str, FieldTypeName] = {
        "brand": "string",
        "size": "string",
        "regular_price": "number",
        "is_deal": "boolean",
        "unit_price": "number",
        "unit_basis": "string",
        "unit_price_text": "string",
        "price_kind": "string",
        "availability": "string",
        "store_verified": "boolean",
        "promo_text": "string",
    }
    return {
        **{name: ContractField(type=t, required=True) for name, t in required.items()},
        **{name: ContractField(type=t) for name, t in optional.items()},
    }


# What the Dispatcher reads from an accepting Site's Records; prices are in dollars.
PRICE_RECORD = _price_record()

_DURATION = re.compile(r"([1-9][0-9]*)([smhd])")
# Coarsest first: a duration is written in the largest unit that divides it.
_UNITS = {
    "d": timedelta(days=1),
    "h": timedelta(hours=1),
    "m": timedelta(minutes=1),
    "s": timedelta(seconds=1),
}
MIN_EVERY = "min_every"  # validation context key: defaults.yaml `schedule.min_every`


def parse_duration(value: Any) -> timedelta:
    if not isinstance(value, str) or not (match := _DURATION.fullmatch(value)):
        raise _config_error(f"`{value}` is not a duration like `30m`, `2h` or `1d`")
    return int(match[1]) * _UNITS[match[2]]


def format_duration(value: timedelta) -> str:
    unit = next(u for u, size in _UNITS.items() if value % size == timedelta(0))
    return f"{value // _UNITS[unit]}{unit}"


# Written as `30m`; snapshots keep that form so Replay parses them back.
Duration = Annotated[timedelta, BeforeValidator(parse_duration), PlainSerializer(format_duration)]


class Schedule(_Model):
    every: Duration
    enabled: bool = True  # false pauses the Site

    @field_validator("every")
    @classmethod
    def _at_least_min_every(cls, every: timedelta, info: ValidationInfo) -> timedelta:
        # Without defaults (a Replay of a snapshot) there is no minimum to apply.
        minimum: timedelta | None = (info.context or {}).get(MIN_EVERY)
        if minimum is not None and every < minimum:
            raise _config_error(
                f"`{format_duration(every)}` is below the minimum of `{format_duration(minimum)}` "
                "(`schedule.min_every` in defaults.yaml)"
            )
        return every


class Site(_Model):
    site: str
    settings: Settings
    # Location name -> its Variables (`location.*`).
    locations: Annotated[dict[str, dict[str, Any]], AfterValidator(_check_locations)] = Field(
        default_factory=dict
    )
    session: Session | None = None
    replay: Replay = Field(default_factory=Replay)
    records: dict[str, RecordType] = Field(default_factory=dict)
    health: Health = Field(default_factory=Health)
    accepts: AcceptsRule | None = None
    schedule: Schedule | None = None
    start: Annotated[list[StartRequest], Field(min_length=1)]
    page_types: Annotated[dict[str, PageType], Field(min_length=1)]

    def resolve_location(self, name: str | None) -> str:
        """The Location a Run scrapes; one must be picked when the Site declares any."""
        if not self.locations:
            if name in (None, DEFAULT_LOCATION):
                return DEFAULT_LOCATION
            raise ValueError(f"unknown Location `{name}`; the Site declares none")
        declared = ", ".join(self.locations)
        if name is None:
            raise ValueError(f"--location is required; the Site declares: {declared}")
        if name not in self.locations:
            raise ValueError(f"unknown Location `{name}`; the Site declares: {declared}")
        return name
