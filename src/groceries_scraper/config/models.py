"""Pydantic models for a Site config (see docs/design.md)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
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
        # Pipe keys sit inline with Field keys (`{css: ..., type: string}`), or the
        # whole Field is a Pipe list.
        if isinstance(data, list):
            return {PIPE_KEY: data}
        if not isinstance(data, dict):
            return data
        own = {k: v for k, v in data.items() if k in FIELD_KEYS}
        step = {k: v for k, v in data.items() if k not in FIELD_KEYS}
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
            # The engine only coerces each match; anything else in `items` would be ignored.
            if self.items is not None and (
                self.items.type in ("object", "array") or self.items.model_fields_set - {"type"}
            ):
                raise _config_error("an array Field's `items` only takes a scalar `type`")
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
TEMPLATE_NAMES = ("value", "session", "env")


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
    record: str | None = None
    items: Loop | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    follow: list[FollowRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _each_scope_has_loop(self) -> PageType:
        if self.items is None and any(rule.scope == "each" for rule in self.follow):
            raise _config_error("a Follow Rule with `scope: each` needs a page-level `items` Loop")
        return self


# --- Site-level sections ----------------------------------------------------


class Settings(_Model):
    """Allowlist of runtime settings; values come from defaults.yaml merged with the Site."""

    download_delay: Annotated[float, Field(ge=0)]
    concurrent_requests_per_domain: Annotated[int, Field(ge=1)]
    obey_robots: bool
    record_level: Literal["all", "errors", "off"]


class SetupStep(_Model):
    request: RequestTemplate
    extract: dict[str, Pipe] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_url(self) -> SetupStep:
        if self.request.url is None:
            raise _config_error("a Session Setup request needs a `url`")
        return self


class Session(_Model):
    setup: list[SetupStep]
    refresh_on: list[int] = Field(default_factory=list)
    max_refresh: Annotated[int, Field(ge=0)] = 1


class Replay(_Model):
    ignore_params: list[str] = Field(default_factory=list)


class RecordType(_Model):
    key: list[str] = Field(default_factory=list)  # a Record Type has at most one Record Key


Ratio = Annotated[float, Field(ge=0, le=1)]


class Health(_Model):
    min_records: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    max_dropped_ratio: Ratio | None = None
    max_null_ratio: dict[str, Ratio] = Field(default_factory=dict)
    max_http_error_ratio: Ratio | None = None


class Site(_Model):
    site: str
    settings: Settings
    session: Session | None = None
    replay: Replay = Field(default_factory=Replay)
    records: dict[str, RecordType] = Field(default_factory=dict)
    health: Health = Field(default_factory=Health)
    start: Annotated[list[StartRequest], Field(min_length=1)]
    page_types: Annotated[dict[str, PageType], Field(min_length=1)]
