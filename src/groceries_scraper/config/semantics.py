"""Semantic checks a schema can't express: cross-references, Scope types, Variable flow."""

import importlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from jinja2 import TemplateSyntaxError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment

from groceries_scraper.config.models import (
    TEMPLATE_NAMES,
    FieldSpec,
    PageType,
    Pipe,
    RequestTemplate,
    Site,
)


@dataclass
class Findings:
    """`"<yaml path>: <message>"` lines; errors block a Run, warnings don't."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_site(site: Site) -> Findings:
    findings = Findings()
    for i, start in enumerate(site.start):
        _check_ref(site, findings, f"start[{i}].page_type", start.page_type)
    for name, page_type in site.page_types.items():
        for j, rule in enumerate(page_type.follow):
            _check_ref(site, findings, f"page_types.{name}.follow[{j}].page_type", rule.page_type)
    reachable = _reachable(site)
    incoming = _incoming_variables(site, reachable)
    setup = site.session.setup if site.session else []
    session = frozenset(name for step in setup for name in step.extract)
    for i, step in enumerate(setup):
        earlier = frozenset(name for s in setup[:i] for name in s.extract)
        checker = _Checker(findings, f"session.setup[{i}]", _no_variables, earlier)
        checker.request(f"session.setup[{i}].request", step.request, frozenset())
        for name, pipe in step.extract.items():
            checker.pipe(f"session.setup[{i}].extract.{name}", pipe, None, False)
    for name, page_type in site.page_types.items():
        missing = _missing_via(name, incoming[name]) if name in reachable else None
        _Checker(findings, f"page_types.{name}", missing, session).check(page_type)
    _check_records(site, findings)
    findings.warnings += [
        f"page_types.{name}: unreachable from any Start Request"
        for name in site.page_types
        if name not in reachable
    ]
    return findings


def _check_ref(site: Site, findings: Findings, path: str, page_type: str) -> None:
    if page_type not in site.page_types:
        findings.errors.append(f"{path}: unknown Page Type `{page_type}`")


def _check_records(site: Site, findings: Findings) -> None:
    emitters = defaultdict(list)
    for name, page_type in site.page_types.items():
        if page_type.record is not None:
            emitters[page_type.record].append(name)
    for record_type, spec in site.records.items():
        if record_type not in emitters:
            findings.errors.append(f"records.{record_type}: {_not_emitted(record_type)}")
        for i, key in enumerate(spec.key):
            findings.errors += [
                f"records.{record_type}.key[{i}]: Page Type `{name}` has no Field `{key}`"
                for name in emitters[record_type]
                if key not in site.page_types[name].fields
            ]
    findings.errors += [
        f"health.min_records.{record_type}: {_not_emitted(record_type)}"
        for record_type in site.health.min_records
        if record_type not in emitters
    ]
    fields = {f for names in emitters.values() for n in names for f in site.page_types[n].fields}
    findings.errors += [
        f"health.max_null_ratio.{name}: no Record-emitting Page Type has Field `{name}`"
        for name in site.health.max_null_ratio
        if name not in fields
    ]


def _not_emitted(record_type: str) -> str:
    return f"no Page Type emits Record Type `{record_type}`"


def _reachable(site: Site) -> set[str]:
    seen: set[str] = set()
    pending = [start.page_type for start in site.start]
    while pending:
        name = pending.pop()
        if name in seen or name not in site.page_types:
            continue
        seen.add(name)
        pending += [rule.page_type for rule in site.page_types[name].follow]
    return seen


Edge = tuple[str, frozenset[str]]  # (yaml path, Variables a request along it carries)


def _incoming_variables(site: Site, reachable: set[str]) -> dict[str, list[Edge]]:
    """Each reachable Page Type's incoming edges, with the Variables every path supplies.

    Variables accumulate down the chain, so a Page Type has those on *all* its incoming
    edges; iterated to a fixpoint because pagination and cycles feed back into themselves.
    """
    available: dict[str, frozenset[str] | None] = dict.fromkeys(reachable)  # None: no path yet
    while True:
        incoming: dict[str, list[Edge]] = defaultdict(list)
        for i, start in enumerate(site.start):
            incoming[start.page_type].append((f"start[{i}]", frozenset()))
        for name, page_type in site.page_types.items():
            if (carried := available.get(name)) is None:
                continue
            for j, rule in enumerate(page_type.follow):
                path = f"page_types.{name}.follow[{j}]"
                incoming[rule.page_type].append((path, carried.union(rule.pass_)))
        updated = {
            name: frozenset.intersection(*(v for _, v in incoming[name]))
            if incoming[name]
            else None
            for name in reachable
        }
        if updated == available:
            return incoming
        available = updated


Missing = Callable[[str], str | None]  # Variable name -> why it's unavailable, or None


def _missing_via(page_type: str, edges: list[Edge]) -> Missing:
    def missing(name: str) -> str | None:
        via = [path for path, variables in edges if name not in variables]
        if not via:
            return None
        return (
            f"Variable `{name}` is not passed on every path to Page Type `{page_type}` "
            f"(missing via {', '.join(via)})"
        )

    return missing


def _no_variables(name: str) -> str:
    return f"Variable `{name}` is not available in Session Setup"


# --- Pipes ------------------------------------------------------------------

# What a Step yields, as far as config can tell; None is unknown and never flagged.
Scope = Literal["HTML", "JSON", "text"] | None

_TEXT_KINDS = (
    "regex",
    "replace",
    "strip",
    "split",
    "join",
    "lower",
    "upper",
    "urljoin",
    "template",
)
# `/` or `//` at the start, optionally inside a parenthesised group: `(//a)[1]`.
_ABSOLUTE_XPATH = re.compile(r"\s*(\(\s*)*/")
_HINTS: dict[tuple[Scope, Scope], str] = {
    ("HTML", "JSON"): " (add `parse: html`)",
    ("JSON", "HTML"): " (add `parse: json`)",
}


_JINJA = SandboxedEnvironment()


@dataclass
class _Checker:
    findings: Findings
    path: str
    missing: Missing | None  # None: unreachable, so no Variables to check against
    session: frozenset[str]  # Session Variables set by Session Setup

    def check(self, page_type: PageType) -> None:
        page: Scope = "JSON" if page_type.response == "json" else "HTML"
        node, looped = page, page_type.items is not None
        if page_type.items is not None:
            node = self.pipe(f"{self.path}.items.each", page_type.items.each, page, False)
        self._fields(f"{self.path}.fields", page_type.fields, node, looped)
        for j, rule in enumerate(page_type.follow):
            path, each = f"{self.path}.follow[{j}]", rule.scope == "each"
            scope = node if each else page
            self.pipe(f"{path}.select", rule.select, scope, each)
            for name, pipe in rule.pass_.items():
                self.pipe(f"{path}.pass.{name}", pipe, scope, each)
            if rule.request is not None:
                local = frozenset([*rule.pass_, *([rule.as_] if rule.as_ else [])])
                self.request(f"{path}.request", rule.request, local)

    def request(self, path: str, template: RequestTemplate, local: frozenset[str]) -> None:
        """`local`: names only this request's templates see (the rule's `pass:` and `as:`)."""
        sources: list[tuple[str, str]] = []
        if template.url is not None:
            sources.append((f"{path}.url", template.url))
        sources += [(f"{path}.headers.{k}", v) for k, v in template.headers.items()]
        sources += [(f"{path}.form.{k}", v) for k, v in (template.form or {}).items()]
        if template.body is not None:
            sources.append((f"{path}.body", template.body))
        sources += _json_strings(f"{path}.json", template.json_body)
        for source_path, source in sources:
            self._template(source_path, source, local)

    def _fields(
        self, prefix: str, specs: dict[str, FieldSpec], scope: Scope, in_loop: bool
    ) -> None:
        for name, spec in specs.items():
            path = f"{prefix}.{name}"
            if spec.each:
                node = self.pipe(f"{path}.each", spec.each, scope, in_loop)
                self._fields(f"{path}.fields", spec.fields, node, True)
            else:
                value = self.pipe(path, spec.pipe, scope, in_loop)
                self._fields(f"{path}.fields", spec.fields, value, in_loop)

    def pipe(self, path: str, pipe: Pipe, scope: Scope, in_loop: bool) -> Scope:
        """Returns the Scope after the last Step; a single Step is addressed without an index."""
        for i, step in enumerate(pipe):
            step_path = path if len(pipe) == 1 else f"{path}[{i}]"
            if step.kind in ("css", "xpath"):
                self._expect(step_path, step.kind, "HTML", scope)
                scope = "HTML"
                if (
                    in_loop
                    and step.xpath
                    and _ABSOLUTE_XPATH.match(step.xpath)
                    and not step.absolute
                ):
                    self._error(
                        step_path,
                        "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`",
                    )
            elif step.kind == "jsonpath":
                self._expect(step_path, step.kind, "JSON", scope)
                scope = "JSON"
            elif step.kind == "parse":
                scope = "JSON" if step.parse == "json" else "HTML"
                in_loop = False  # a fresh document: `//` is relative to it
            elif step.kind in _TEXT_KINDS:
                if step.template is not None:
                    self._template(step_path, step.template, frozenset())
                scope = "text"
            else:  # var, fn: any value
                if step.var is not None:
                    self._variable(step_path, step.var)
                if step.fn is not None and (reason := _unresolvable(step.fn)):
                    self._error(step_path, reason)
                scope = None
        return scope

    def _template(self, path: str, source: str, local: frozenset[str]) -> None:
        try:
            ast = _JINJA.parse(source)
        except TemplateSyntaxError as exc:
            self._error(path, f"invalid template: {exc.message}")
            return
        names = meta.find_undeclared_variables(ast) - set(TEMPLATE_NAMES) - local
        session = {
            f"session.{node.attr}"
            for node in ast.find_all(nodes.Getattr)
            if isinstance(node.node, nodes.Name) and node.node.name == "session"
        }
        for name in sorted(names | session):
            self._variable(path, name)

    def _variable(self, path: str, name: str) -> None:
        if name.startswith("session."):
            if name.removeprefix("session.") not in self.session:
                self._error(path, f"Session Variable `{name}` is not set by Session Setup")
        elif self.missing is not None and (reason := self.missing(name)):
            self._error(path, reason)

    def _expect(self, path: str, kind: str, wanted: Scope, scope: Scope) -> None:
        if scope is not None and scope != wanted:
            a = "an" if wanted == "HTML" else "a"
            hint = _HINTS.get((wanted, scope), "")
            self._error(path, f"{kind} needs {a} {wanted} Scope, got {scope}{hint}")

    def _error(self, path: str, message: str) -> None:
        self.findings.errors.append(f"{path}: {message}")


def _json_strings(path: str, value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _json_strings(f"{path}.{key}", item)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _json_strings(f"{path}[{i}]", item)


def _unresolvable(ref: str) -> str | None:
    """Resolves `module:callable` as the engine will, so a typo fails before a Run."""
    module_name, _, name = ref.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # any import-time failure would also break the Run
        return f"cannot import `{module_name}`: {exc}"
    if not hasattr(module, name):
        return f"`{module_name}` has no attribute `{name}`"
    if not callable(getattr(module, name)):
        return f"`{ref}` is not callable"
    return None
