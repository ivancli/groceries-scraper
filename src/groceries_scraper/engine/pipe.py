"""Pipe evaluation. A Scope is a parsel `Selector` (HTML) or any other value (JSON)."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any, NamedTuple
from urllib.parse import urljoin

from jinja2 import StrictUndefined, Template, Undefined
from jinja2.sandbox import SandboxedEnvironment
from jsonpath_ng import JSONPath
from jsonpath_ng.ext import parse as parse_jsonpath
from parsel import Selector

from groceries_scraper.config.models import STEP_KINDS, Pipe, Step, resolve_fn


@dataclass(frozen=True)
class PipeContext:
    variables: Mapping[str, Any] = field(default_factory=dict)
    session: Mapping[str, Any] = field(default_factory=dict)
    location: Mapping[str, Any] = field(default_factory=dict)  # fixed for the Run
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None


@dataclass(frozen=True)
class StepTrace:
    step: str
    output: list[Any]
    error: str | None = None


class PipeResult(NamedTuple):
    values: list[Any]
    trace: list[StepTrace]


class StepError(Exception):
    pass


def run_pipe(pipe: Pipe, scope: Any, ctx: PipeContext) -> PipeResult:
    """Run each Step over all current values; the first error ends the Pipe with no value."""
    values = [scope]
    trace: list[StepTrace] = []
    for step in pipe:
        try:
            values = _STEPS[step.kind](step, values, ctx)
        except Exception as exc:  # a Step must never crash extraction
            trace.append(StepTrace(step.kind, [], _describe(exc)))
            return PipeResult([], trace)
        trace.append(StepTrace(step.kind, [_traceable(v) for v in values]))
    return PipeResult(values, trace)


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, StepError) else f"{type(exc).__name__}: {exc}"


def _traceable(value: Any) -> Any:
    return value.get() if isinstance(value, Selector) else value


# --- Selector steps ---------------------------------------------------------


def _html(value: Any, kind: str) -> Selector:
    if not isinstance(value, Selector):
        raise StepError(f"{kind} needs an HTML Scope, got {type(value).__name__}")
    return value


def _node_or_text(match: Selector) -> Any:
    # ::text / ::attr() / @attr and XPath count()/boolean() matches are values, not nodes.
    return match.root if isinstance(match.root, str | float | bool) else match


def _css(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.css is not None
    return [_node_or_text(m) for v in values for m in _html(v, "css").css(step.css)]


def _xpath(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.xpath is not None
    return [_node_or_text(m) for v in values for m in _html(v, "xpath").xpath(step.xpath)]


def _jsonpath(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.jsonpath is not None
    expr = _compiled_jsonpath(step.jsonpath)
    return [m.value for v in values for m in expr.find(_json(v))]


@cache
def _compiled_jsonpath(source: str) -> JSONPath:
    return parse_jsonpath(source)


def _json(value: Any) -> Any:
    # A JSON string Scope can't match anything useful; it's almost always unparsed text.
    if isinstance(value, Selector | str):
        got = "HTML" if isinstance(value, Selector) else "text"
        raise StepError(f"jsonpath needs a JSON Scope, got {got} (add `parse: json`)")
    return value


def _parse(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    if step.parse == "json":
        return [json.loads(_text(v, "parse")) for v in values]
    return [Selector(text=_text(v, "parse")) for v in values]


# --- Text transforms --------------------------------------------------------


def _text(value: Any, kind: str) -> str:
    if isinstance(value, Selector):
        return str(value.xpath("string()").get())
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    raise StepError(f"{kind} needs text, got {type(value).__name__}")


def _each_text(kind: str, fn: Callable[[str], str]) -> Callable[..., list[Any]]:
    def step(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
        return [fn(_text(v, kind)) for v in values]

    return step


def _regex(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.regex is not None
    pattern = re.compile(step.regex)
    matches = (pattern.search(_text(v, "regex")) for v in values)
    return [m.group(1) if pattern.groups else m.group(0) for m in matches if m]


def _replace(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.replace is not None
    old, new = step.replace
    return [_text(v, "replace").replace(old, new) for v in values]


def _split(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.split is not None
    return [part for v in values for part in _text(v, "split").split(step.split)]


def _join(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.join is not None
    return [step.join.join(_text(v, "join") for v in values)] if values else []


def _urljoin(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    if values and ctx.url is None:
        raise StepError("urljoin needs the response URL")
    return [urljoin(ctx.url or "", _text(v, "urljoin")) for v in values]


# --- Variables, templates and custom functions -------------------------------


def _var(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.var is not None
    source, name = ctx.variables, step.var
    if name.startswith("session."):
        source, name = ctx.session, name.removeprefix("session.")
    elif name.startswith("location."):
        source, name = ctx.location, name.removeprefix("location.")
    if name not in source:
        raise StepError(f"unknown Variable `{step.var}`")
    return [source[name]]


_JINJA = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


@cache
def _compiled_template(source: str) -> Template:
    return _JINJA.from_string(source)


def render(
    source: str, value: Any, ctx: PipeContext, names: Mapping[str, Any] | None = None
) -> str:
    """Render a sandboxed template with `value`, Variables, `session` and `env` in scope."""
    return _compiled_template(source).render(template_scope(value, ctx, names))


# A whole string that is one `{{ expr }}`, with no other text or expressions.
_ONE_EXPRESSION = re.compile(r"\{\{((?:(?!\{\{|\}\}).)*)\}\}", re.S)


def render_native(
    source: str, value: Any, ctx: PipeContext, names: Mapping[str, Any] | None = None
) -> Any:
    """Like `render`, but a lone `{{ expr }}` keeps its type, so JSON APIs get 2, not "2"."""
    match = _ONE_EXPRESSION.fullmatch(source)
    if match is None:
        return render(source, value, ctx, names)
    result = _compiled_expression(match.group(1))(**template_scope(value, ctx, names))
    if isinstance(result, Undefined):
        str(result)  # raises UndefinedError, as `render` would
    return result


@cache
def _compiled_expression(source: str) -> Callable[..., Any]:
    return _JINJA.compile_expression(source, undefined_to_none=False)


def template_scope(
    value: Any, ctx: PipeContext, names: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    # Reserved names go last so a Variable can never shadow them; config's TEMPLATE_NAMES
    # must list exactly these keys (enforced by a test).
    reserved = {
        "value": plain(value),
        "session": ctx.session,
        "location": ctx.location,
        "env": ctx.env,
    }
    return {**ctx.variables, **(names or {}), **reserved}


def _template(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.template is not None
    return [render(step.template, v, ctx) for v in values]


def plain(value: Any) -> Any:
    """Templates and Variables want an HTML node's text, never the parsel node itself."""
    return _text(value, "template") if isinstance(value, Selector) else value


_resolved_fn = cache(resolve_fn)


def _fn(step: Step, values: list[Any], ctx: PipeContext) -> list[Any]:
    assert step.fn is not None
    fn = _resolved_fn(step.fn)
    return [fn(v, ctx) for v in values]


_STEPS: dict[str, Callable[[Step, list[Any], PipeContext], list[Any]]] = {
    "css": _css,
    "xpath": _xpath,
    "jsonpath": _jsonpath,
    "parse": _parse,
    "regex": _regex,
    "replace": _replace,
    "strip": _each_text("strip", str.strip),
    "split": _split,
    "join": _join,
    "lower": _each_text("lower", str.lower),
    "upper": _each_text("upper", str.upper),
    "urljoin": _urljoin,
    "var": _var,
    "template": _template,
    "fn": _fn,
}
assert set(_STEPS) == set(STEP_KINDS)  # a new Step kind needs an implementation here
