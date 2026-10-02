"""Request Templates rendered to HTTP requests — descriptions only; the adapter sends them."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode, urljoin

from groceries_scraper.config.models import RequestTemplate
from groceries_scraper.engine.pipe import PipeContext, render, render_native


@dataclass(frozen=True)
class RenderedRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None


@dataclass(frozen=True)
class RequestSource:
    """Everything a Request Template renders from; kept so a retry can re-render it."""

    template: RequestTemplate
    value: Any  # the Follow Rule's selected value; None in Session Setup
    ctx: PipeContext
    bindings: Mapping[str, Any] = field(default_factory=dict)  # a Follow Rule's `as:` name

    def with_session(self, session: Mapping[str, Any]) -> "RequestSource":
        return replace(self, ctx=replace(self.ctx, session=session))

    def render(self) -> RenderedRequest:
        """No template `url:` means `value` is the URL; relative URLs resolve against `ctx.url`."""
        template = self.template

        def fill(source: str) -> str:
            return render(source, self.value, self.ctx, self.bindings)

        def fill_native(source: str) -> Any:
            return render_native(source, self.value, self.ctx, self.bindings)

        if template.url is not None:
            url = fill(template.url)
        elif isinstance(self.value, str):
            url = self.value
        else:
            raise ValueError(f"selected value is not a URL: {self.value!r}")
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
        return RenderedRequest(template.method, urljoin(self.ctx.url or "", url), headers, body)


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
