"""Follow Rules: turn one response into Follow Requests — descriptions only, never I/O."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple
from urllib.parse import urlencode, urljoin

from groceries_scraper.config.models import FollowRule, PageType, RequestTemplate
from groceries_scraper.engine.pipe import (
    PipeContext,
    StepTrace,
    plain,
    render,
    render_native,
    run_pipe,
)


@dataclass(frozen=True)
class FollowRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    page_type: str
    variables: dict[str, Any]
    parent_ref: int | None  # the Capture this request was discovered in
    # What `rerender` needs; None for requests with nothing to render (Start Requests).
    source: "RequestSource | None" = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class RequestSource:
    template: RequestTemplate
    value: Any
    ctx: PipeContext
    names: Mapping[str, Any]


def rerender(request: FollowRequest, session: Mapping[str, Any]) -> FollowRequest:
    """Retries after a Session refresh must carry the new Session Variables."""
    if request.source is None:
        return request
    source = replace(request.source, ctx=replace(request.source.ctx, session=session))
    rendered = render_request(source.template, source.value, source.ctx, source.names)
    return replace(request, **rendered._asdict(), source=source)


@dataclass(frozen=True)
class FollowTrace:
    rule: int
    path: str  # select | pass.<name> | request
    steps: list[StepTrace]
    error: str | None = None
    node: int | None = None  # None: page scope


@dataclass(frozen=True)
class FollowResult:
    requests: list[FollowRequest] = field(default_factory=list)
    trace: list[FollowTrace] = field(default_factory=list)


def follow(
    page_type: PageType,
    scope: Any,
    nodes: list[Any],
    ctx: PipeContext,
    parent_ref: int | None = None,
) -> FollowResult:
    """`scope: each` rules run over `nodes`, the page-level Loop's matches."""
    result = FollowResult()
    for index, rule in enumerate(page_type.follow):
        evaluator = _RuleEvaluator(index, rule, ctx, parent_ref, result)
        if rule.scope == "page":
            evaluator.evaluate(scope, None)
        else:
            for node_index, node in enumerate(nodes):
                evaluator.evaluate(node, node_index)
    return result


@dataclass
class _RuleEvaluator:
    index: int
    rule: FollowRule
    ctx: PipeContext
    parent_ref: int | None
    result: FollowResult

    def evaluate(self, scope: Any, node: int | None) -> None:
        selected = self._run("select", self.rule.select, scope, node)
        if not selected:
            return
        passed = {}
        for name, pipe in self.rule.pass_.items():
            values = self._run(f"pass.{name}", pipe, scope, node)
            passed[name] = plain(values[0]) if values else None
        child_ctx = replace(self.ctx, variables={**self.ctx.variables, **passed})
        for value in selected:
            try:
                request = self._request(plain(value), child_ctx)
            except Exception as exc:  # one bad render must not stop other requests or rules
                self._trace("request", [], node, f"{type(exc).__name__}: {exc}")
            else:
                self.result.requests.append(request)

    def _request(self, value: Any, child_ctx: PipeContext) -> FollowRequest:
        template = self.rule.request or RequestTemplate()
        names = {self.rule.as_: value} if self.rule.as_ else {}
        return FollowRequest(
            *render_request(template, value, child_ctx, names),
            page_type=self.rule.page_type,
            variables=dict(child_ctx.variables),
            parent_ref=self.parent_ref,
            source=RequestSource(template, value, child_ctx, names),
        )

    def _run(self, path: str, pipe: list[Any], scope: Any, node: int | None) -> list[Any]:
        values, steps = run_pipe(pipe, scope, self.ctx)
        self._trace(path, steps, node)
        return values

    def _trace(
        self, path: str, steps: list[StepTrace], node: int | None, error: str | None = None
    ) -> None:
        self.result.trace.append(FollowTrace(self.index, path, steps, error, node))


class RenderedRequest(NamedTuple):
    method: str
    url: str
    headers: dict[str, str]
    body: str | None


def render_request(
    template: RequestTemplate,
    value: Any,
    ctx: PipeContext,
    names: Mapping[str, Any] | None = None,
) -> RenderedRequest:
    """No template `url:` means `value` is the URL; relative URLs resolve against `ctx.url`."""

    def fill(source: str) -> str:
        return render(source, value, ctx, names)

    def fill_native(source: str) -> Any:
        return render_native(source, value, ctx, names)

    if template.url is not None:
        url = fill(template.url)
    elif isinstance(value, str):
        url = value
    else:
        raise ValueError(f"selected value is not a URL: {value!r}")
    headers = {name: fill(v) for name, v in template.headers.items()}
    body = None
    if template.json_body is not None:
        body = json.dumps(_render_json(template.json_body, fill_native))
        _default_content_type(headers, "application/json")
    elif template.form is not None:
        body = urlencode({name: fill(v) for name, v in template.form.items()})
        _default_content_type(headers, "application/x-www-form-urlencoded")
    elif template.body is not None:
        body = fill(template.body)
    return RenderedRequest(template.method, urljoin(ctx.url or "", url), headers, body)


def _render_json(value: Any, fill: Callable[[str], Any]) -> Any:
    """Keys stay as written: they're the API's field names, not data."""
    if isinstance(value, str):
        return fill(value)
    if isinstance(value, dict):
        return {key: _render_json(v, fill) for key, v in value.items()}
    if isinstance(value, list):
        return [_render_json(v, fill) for v in value]
    return value


def _default_content_type(headers: dict[str, str], content_type: str) -> None:
    if not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = content_type
