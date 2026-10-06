"""Semantic checks a schema can't express: cross-references, Scope types, Variable flow."""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from jinja2 import TemplateSyntaxError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment

from groceries_scraper.config.models import (
    PRICE_RECORD,
    STEP_KINDS,
    TEMPLATE_NAMES,
    ContractField,
    FieldSpec,
    PageType,
    Pipe,
    RequestTemplate,
    Site,
    Step,
    resolve_fn,
)


@dataclass
class Findings:
    """`"<yaml path>: <message>"` lines; errors block a Run, warnings don't."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_site(site: Site) -> Findings:
    findings = Findings()
    _check_refs(site, findings)
    reachable = _reachable(site)
    session = _check_session_setup(site, findings)
    incoming = _incoming_variables(site, reachable)
    for name, page_type in site.page_types.items():
        why = _why_unpassed(name, incoming[name]) if name in reachable else None
        _Checker(findings, f"page_types.{name}", why, session, site.locations).check(page_type)
    _check_records(site, findings, reachable)
    _check_accepts(site, findings)
    if site.session and site.session.pool > len(site.start):
        # Follow-on requests keep their Start Request's Session.
        starts = f"{len(site.start)} Start Request{'s' if len(site.start) > 1 else ''}"
        findings.warnings.append(
            f"session.pool: {site.session.pool} Sessions but {starts}; "
            "Sessions without a Start Request stay idle"
        )
    findings.warnings += [
        f"page_types.{name}: unreachable from any Start Request"
        for name in site.page_types
        if name not in reachable
    ]
    return findings


def _check_refs(site: Site, findings: Findings) -> None:
    refs = [(f"start[{i}].page_type", start.page_type) for i, start in enumerate(site.start)]
    refs += [
        (f"page_types.{name}.follow[{j}].page_type", rule.page_type)
        for name, page_type in site.page_types.items()
        for j, rule in enumerate(page_type.follow)
    ]
    if site.accepts is not None:
        refs.append(("accepts.page_type", site.accepts.page_type))
    findings.errors += [
        f"{path}: unknown Page Type `{target}`"
        for path, target in refs
        if target not in site.page_types
    ]


def _check_accepts(site: Site, findings: Findings) -> None:
    accepts = site.accepts
    if accepts is None:
        if site.schedule is not None:
            findings.errors.append(
                "schedule: a Site with a Schedule needs `accepts`; "
                "the Dispatcher only schedules Supplied Start Requests"
            )
        return
    if site.schedule is None:
        findings.errors.append("accepts: a Site with an Accepts Rule needs a `schedule`")
    findings.errors += [
        f"accepts.examples[{i}]: `{url}` does not match `accepts.url`"
        for i, url in enumerate(accepts.examples)
        if not accepts.matches(url)
    ]
    page_type = site.page_types.get(accepts.page_type)
    if page_type is not None and page_type.follow:
        findings.errors.append(
            f"accepts.page_type: Page Type `{accepts.page_type}` has Follow Rules; "
            "a Supplied Start Request must not start a crawl"
        )
    if page_type is not None:
        _check_price_record(accepts.page_type, page_type, findings)


def _check_price_record(name: str, page_type: PageType, findings: Findings) -> None:
    """The Dispatcher reads only these Fields, so others are allowed."""
    if page_type.record is None:
        findings.errors.append(
            f"accepts.page_type: Page Type `{name}` emits no Records; "
            "an accepting Site's Records must match the Price Record contract"
        )
        return
    contract = _Contract(findings, "the Price Record contract")
    for field_name, wanted in PRICE_RECORD.items():
        path = f"page_types.{name}.fields.{field_name}"
        if field_name in page_type.fields:
            contract.field(path, wanted, page_type.fields[field_name])
        elif wanted.required:
            findings.errors.append(f"{path}: missing; the Price Record contract requires it")


def check_sites(sites: Mapping[str, Site]) -> list[str]:
    """Errors across Sites, keyed by source: regex overlap is undecidable, so examples stand in."""
    accepting = {source: site for source, site in sites.items() if site.accepts is not None}
    return [
        f"{source}: accepts.examples[{i}]: `{url}` is also accepted by Site `{other.site}` "
        f"({other_source})"
        for source, site in accepting.items()
        for i, url in enumerate(site.accepts.examples)  # type: ignore[union-attr]
        for other_source, other in accepting.items()
        if other_source != source and other.accepts.matches(url)  # type: ignore[union-attr]
    ]


def _start_page_types(site: Site) -> list[tuple[str, str]]:
    """(yaml path, Page Type) of everything a Run can start at."""
    starts = [(f"start[{i}]", start.page_type) for i, start in enumerate(site.start)]
    if site.accepts is not None:
        starts.append(("accepts", site.accepts.page_type))
    return starts


def _check_session_setup(site: Site, findings: Findings) -> frozenset[str]:
    """Returns the Session Variables it sets; each step sees only earlier steps' ones."""
    setup = site.session.setup if site.session else []
    session: frozenset[str] = frozenset()
    for i, step in enumerate(setup):
        checker = _Checker(findings, f"session.setup[{i}]", _not_in_setup, session, site.locations)
        checker.request(f"session.setup[{i}].request", step.request, frozenset())
        for name, pipe in step.extract.items():
            checker.pipe(f"session.setup[{i}].extract.{name}", pipe, None, False)
        session = session.union(step.extract)
    return session


def _check_records(site: Site, findings: Findings, reachable: set[str]) -> None:
    # Unreachable Page Types never run, so they can't satisfy a Record Type.
    emitters = defaultdict(list)
    for name in reachable:
        if (record_type := site.page_types[name].record) is not None:
            emitters[record_type].append(name)
    findings.errors += [
        f"records.{record_type}: {_not_emitted(record_type)}"
        for record_type in site.records
        if record_type not in emitters
    ]
    findings.errors += [
        f"page_types.{name}.record: Record Type `{page_type.record}` is not declared in `records:`"
        for name, page_type in site.page_types.items()
        if page_type.record is not None and page_type.record not in site.records
    ]
    findings.errors += [
        f"records.{record_type}.key[{i}]: Page Type `{name}` has no Field `{key}`"
        for record_type, spec in site.records.items()
        for i, key in enumerate(spec.key)
        for name in sorted(emitters[record_type])
        if key not in site.page_types[name].fields
    ]
    for record_type, spec in site.records.items():
        if spec.fields is None:
            continue
        contract = _Contract(findings, f"Record Type `{record_type}`'s contract")
        for name in sorted(emitters[record_type]):
            contract.check(f"page_types.{name}.fields", spec.fields, site.page_types[name].fields)
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


@dataclass
class _Contract:
    """Checks a Page Type's Fields against its Record Type's Record Contract."""

    findings: Findings
    label: str  # names the contract in messages

    def check(
        self, prefix: str, contract: dict[str, ContractField], specs: dict[str, FieldSpec]
    ) -> None:
        for name, wanted in contract.items():
            if name in specs:
                self.field(f"{prefix}.{name}", wanted, specs[name])
            else:
                self._error(f"{prefix}.{name}", f"missing; {self.label} declares it")
        for name in specs:
            if name not in contract:
                self._error(f"{prefix}.{name}", f"not in {self.label}")

    def field(self, path: str, wanted: ContractField, spec: FieldSpec) -> None:
        if spec.type is None:
            # Untyped Fields skip Coercion, so nothing would enforce the contract's type.
            fix = f"add `type: {wanted.type}` to match {self.label}"
            self._error(path, f"no type; {fix}")
            return
        if spec.type != wanted.type:
            self._error(path, f"type `{spec.type}`, but {self.label} says `{wanted.type}`")
            return  # nested Fields of a different type would only add noise
        # A stricter emitter is fine: optional in the contract only allows nulls.
        if wanted.required and not spec.required:
            self._error(path, f"{self.label} requires `required: true`")
        if (wanted.items is None) != (spec.items is None):
            got, says = _array_shape(spec.items), _array_shape(wanted.items)
            self._error(path, f"{got}, but {self.label} says {says}")
        elif wanted.items is not None and spec.items is not None:
            self.field(f"{path}.items", wanted.items, spec.items)
        else:
            self.check(f"{path}.fields", wanted.fields, spec.fields)

    def _error(self, path: str, message: str) -> None:
        self.findings.errors.append(f"{path}: {message}")


def _array_shape(items: object) -> str:
    return "array of scalars (`items`)" if items is not None else "array of objects (`fields`)"


def _not_emitted(record_type: str) -> str:
    return f"no reachable Page Type emits Record Type `{record_type}`"


def _reachable(site: Site) -> set[str]:
    seen: set[str] = set()
    pending = [page_type for _, page_type in _start_page_types(site)]
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
        for path, start_page_type in _start_page_types(site):
            incoming[start_page_type].append((path, frozenset()))
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


WhyUnavailable = Callable[[str], str | None]  # Variable name -> the error, or None if available


def _why_unpassed(page_type: str, edges: list[Edge]) -> WhyUnavailable:
    def why(name: str) -> str | None:
        via = [path for path, variables in edges if name not in variables]
        if not via:
            return None
        return (
            f"Variable `{name}` is not passed on every path to Page Type `{page_type}` "
            f"(missing via {', '.join(via)})"
        )

    return why


def _not_in_setup(name: str) -> str:
    return f"Variable `{name}` is not available in Session Setup"


# --- Pipes ------------------------------------------------------------------

# What a Step yields, as far as config can tell; None is unknown and never flagged.
ValueKind = Literal["HTML", "JSON", "text"] | None

_SELECTOR_KINDS = ("css", "xpath", "jsonpath")
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
_ANY_KINDS = ("var", "fn")
# A Step kind validation doesn't know would silently skip its checks.
assert {*_SELECTOR_KINDS, *_TEXT_KINDS, *_ANY_KINDS, "parse"} == set(STEP_KINDS)

# `/` or `//` at the start, optionally inside a parenthesised group: `(//a)[1]`.
_ABSOLUTE_XPATH = re.compile(r"\s*(\(\s*)*/")
# Selectors whose matches the engine returns as strings, not nodes (see `_node_or_text`).
_CSS_TEXT = re.compile(r"::(text|attr\([^)]*\))\s*$")
_XPATH_TEXT = re.compile(
    r"((^|/)\s*@[\w:.*-]+|/\s*text\(\s*\))\s*$"
    r"|^\s*(count|string|normalize-space|concat|substring[\w-]*|string-length|sum|number"
    r"|boolean|translate|name|local-name)\s*\(",
)
_HINTS: dict[tuple[ValueKind, ValueKind], str] = {
    ("HTML", "JSON"): " (add `parse: html`)",
    ("JSON", "HTML"): " (add `parse: json`)",
}

_JINJA = SandboxedEnvironment()


@dataclass
class _Checker:
    findings: Findings
    path: str
    why_unavailable: WhyUnavailable | None  # None: unreachable, so no Variables to check
    session: frozenset[str]  # Session Variables set by Session Setup
    locations: Mapping[str, Mapping[str, Any]]

    def check(self, page_type: PageType) -> None:
        page_kind: ValueKind = "JSON" if page_type.response == "json" else "HTML"
        loop_kind, looped = page_kind, page_type.items is not None
        if page_type.items is not None:
            loop_kind = self.pipe(f"{self.path}.items.each", page_type.items.each, page_kind, False)
        self._fields(f"{self.path}.fields", page_type.fields, loop_kind, looped)
        for j, rule in enumerate(page_type.follow):
            path, each = f"{self.path}.follow[{j}]", rule.scope == "each"
            kind = loop_kind if each else page_kind
            self.pipe(f"{path}.select", rule.select, kind, each)
            for name, pipe in rule.pass_.items():
                self.pipe(f"{path}.pass.{name}", pipe, kind, each)
            if rule.request is not None:
                self.request(f"{path}.request", rule.request, rule.template_names)

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
        self, prefix: str, specs: dict[str, FieldSpec], kind: ValueKind, in_loop: bool
    ) -> None:
        for name, spec in specs.items():
            path = f"{prefix}.{name}"
            if spec.each:
                node_kind = self.pipe(f"{path}.each", spec.each, kind, in_loop)
                self._fields(f"{path}.fields", spec.fields, node_kind, True)
            else:
                value_kind = self.pipe(path, spec.pipe, kind, in_loop)
                self._fields(f"{path}.fields", spec.fields, value_kind, in_loop)

    def pipe(self, path: str, pipe: Pipe, kind: ValueKind, in_loop: bool) -> ValueKind:
        """A single Step is addressed without an index, matching its shorthand in the YAML."""
        for i, step in enumerate(pipe):
            step_path = path if len(pipe) == 1 else f"{path}[{i}]"
            if step.kind in _SELECTOR_KINDS:
                kind = self._selector(step_path, step, kind, in_loop)
            elif step.kind == "parse":
                kind = "JSON" if step.parse == "json" else "HTML"
                in_loop = False  # a fresh document: `//` is relative to it
            elif step.kind in _TEXT_KINDS:
                if step.template is not None:
                    self._template(step_path, step.template, frozenset())
                kind = "text"
            else:  # _ANY_KINDS
                if step.var is not None:
                    self._variable(step_path, step.var)
                if step.fn is not None:
                    self._fn(step_path, step.fn)
                kind = None
        return kind

    def _selector(self, path: str, step: Step, kind: ValueKind, in_loop: bool) -> ValueKind:
        if step.jsonpath is not None:
            self._expect(path, "jsonpath", "JSON", kind)
            return "JSON"
        self._expect(path, step.kind, "HTML", kind)
        if step.css is not None:
            return "text" if _CSS_TEXT.search(step.css) else "HTML"
        assert step.xpath is not None
        branches = _xpath_branches(step.xpath)
        if in_loop and not step.absolute and any(_ABSOLUTE_XPATH.match(b) for b in branches):
            self._error(
                path, "absolute XPath inside a Loop Scope; use `.//` or set `absolute: true`"
            )
        if len(branches) > 1:
            return None  # a union may mix nodes and text
        return "text" if _XPATH_TEXT.search(step.xpath) else "HTML"

    def _fn(self, path: str, ref: str) -> None:
        try:
            resolve_fn(ref)
        except Exception as exc:  # any import-time failure would also break the Run
            self._error(path, f"cannot load `{ref}`: {type(exc).__name__}: {exc}")

    def _template(self, path: str, source: str, local: frozenset[str]) -> None:
        try:
            ast = _JINJA.parse(source)
            _JINJA.compile(ast)  # unknown filters and tests only fail here, not in parse
        except TemplateSyntaxError as exc:
            self._error(path, f"invalid template: {exc.message}")
            return
        names = meta.find_undeclared_variables(ast) - set(TEMPLATE_NAMES) - local
        scoped = {
            f"{scope}.{key}"
            for node in ast.find_all((nodes.Getattr, nodes.Getitem))
            for scope in ("session", "location")
            if (key := _scoped_key(node, scope)) is not None
        }
        for name in sorted(names | scoped):
            self._variable(path, name)

    def _variable(self, path: str, name: str) -> None:
        if name.startswith("session."):
            if name.removeprefix("session.") not in self.session:
                self._error(path, f"Session Variable `{name}` is not set by Session Setup")
        elif name.startswith("location."):
            self._location_variable(path, name)
        elif self.why_unavailable is not None and (reason := self.why_unavailable(name)):
            self._error(path, reason)

    def _location_variable(self, path: str, name: str) -> None:
        # A Run may pick any Location, so every one must set it.
        if not self.locations:
            self._error(
                path, f"Location Variable `{name}` is not set: the Site declares no Locations"
            )
            return
        key = name.removeprefix("location.")
        if missing := [
            location for location, values in self.locations.items() if key not in values
        ]:
            plural = "s" if len(missing) > 1 else ""
            names = ", ".join(f"`{location}`" for location in missing)
            self._error(path, f"Location Variable `{name}` is not set by Location{plural} {names}")

    def _expect(self, path: str, kind: str, wanted: ValueKind, got: ValueKind) -> None:
        if got is not None and got != wanted:
            a = "an" if wanted == "HTML" else "a"
            hint = _HINTS.get((wanted, got), "")
            self._error(path, f"{kind} needs {a} {wanted} Scope, got {got}{hint}")

    def _error(self, path: str, message: str) -> None:
        self.findings.errors.append(f"{path}: {message}")


def _xpath_branches(xpath: str) -> list[str]:
    """Splits a union on top-level `|`, ignoring any inside brackets or string literals."""
    branches, start, depth, quote = [], 0, 0, ""
    for i, char in enumerate(xpath):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "|" and depth == 0:
            branches.append(xpath[start:i])
            start = i + 1
    return [*branches, xpath[start:]]


def _scoped_key(node: nodes.Node, scope: str) -> str | None:
    """The key of `<scope>.x` or `<scope>["x"]`; a computed key can't be checked."""
    if not (isinstance(node, nodes.Getattr | nodes.Getitem) and isinstance(node.node, nodes.Name)):
        return None
    if node.node.name != scope:
        return None
    if isinstance(node, nodes.Getattr):
        return node.attr
    return node.arg.value if isinstance(node.arg, nodes.Const) else None


def _json_strings(path: str, value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _json_strings(f"{path}.{key}", item)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _json_strings(f"{path}[{i}]", item)
