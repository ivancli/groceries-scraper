"""Pydantic models for a Site config (see docs/design.md)."""

from __future__ import annotations

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

OPERATORS = (
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
NO_ARG_OPERATORS = ("strip", "lower", "upper", "urljoin")


class Step(_Model):
    css: str | None = None
    xpath: str | None = None
    jsonpath: str | None = None
    parse: Literal["json", "html"] | None = None
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
    def _bare_operator(cls, data: Any) -> Any:
        # `- strip` in a Pipe list means `{strip: true}`.
        if isinstance(data, str) and data in NO_ARG_OPERATORS:
            return {data: True}
        return data

    @model_validator(mode="after")
    def _exactly_one_operator(self) -> Step:
        ops = [name for name in OPERATORS if getattr(self, name) is not None]
        if len(ops) != 1:
            found = ", ".join(ops) or "none"
            raise _config_error(
                f"a Step needs exactly one operator key ({', '.join(OPERATORS)}); found: {found}"
            )
        return self

    @property
    def op(self) -> str:
        return next(name for name in OPERATORS if getattr(self, name) is not None)


def _as_list(value: Step | list[Step]) -> list[Step]:
    return value if isinstance(value, list) else [value]


# A single Step mapping is shorthand for a one-step Pipe; tagging the two forms keeps the
# list index out of error paths for the shorthand.
if TYPE_CHECKING:
    Pipe = list[Step]
else:
    Pipe = Annotated[
        Annotated[Step, Tag(STEP_TAG)] | Annotated[list[Step], Tag(STEPS_TAG)],
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
    pipe: Pipe = Field(default_factory=list, alias="<pipe>")  # == PIPE_KEY
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

    @model_validator(mode="after")
    def _one_body(self) -> RequestTemplate:
        if self.json_body is not None and self.form is not None:
            raise _config_error("a Request Template takes `json` or `form`, not both")
        return self


class StartRequest(_Model):
    url: str
    page_type: str


class Loop(_Model):
    each: Pipe


class FollowRule(_Model):
    select: Pipe
    scope: Literal["page", "each"] = "page"
    page_type: str
    pass_: dict[str, Pipe] = Field(default_factory=dict, alias="pass")
    request: RequestTemplate | None = None


class PageType(_Model):
    response: Literal["html", "json"] = "html"
    record: str | None = None
    items: Loop | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    follow: list[FollowRule] = Field(default_factory=list)


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
    key: Annotated[list[str], Field(min_length=1)]


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
