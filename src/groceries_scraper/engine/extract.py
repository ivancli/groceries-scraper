"""Field evaluation: turns a Page Type's fields over one response into Records."""

import re
from dataclasses import dataclass, field
from typing import Any

from parsel import Selector

from groceries_scraper.config.models import FieldSpec, PageType, Pipe
from groceries_scraper.engine.pipe import PipeContext, StepTrace, run_pipe


@dataclass(frozen=True)
class Record:
    record_type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class DroppedRecord:
    index: int
    reason: str


@dataclass(frozen=True)
class FieldTrace:
    path: str
    record: int
    steps: list[StepTrace]
    error: str | None = None


@dataclass(frozen=True)
class ExtractionResult:
    records: list[Record] = field(default_factory=list)
    dropped: list[DroppedRecord] = field(default_factory=list)
    trace: list[FieldTrace] = field(default_factory=list)


def extract(page_type: PageType, scopes: list[Any], ctx: PipeContext) -> ExtractionResult:
    """`scopes` holds one Scope per Record: the Loop's nodes, or just the response."""
    if page_type.record is None:
        return ExtractionResult()
    result = ExtractionResult()
    for index, node in enumerate(scopes):
        fields = _Fields(ctx, result.trace, index)
        data = fields.evaluate(page_type.fields, node, "")
        if fields.missing:
            result.dropped.append(DroppedRecord(index, "; ".join(fields.missing)))
        else:
            result.records.append(Record(page_type.record, data))
    return result


# Distinct from None so `default` applies only when nothing matched, not after a failed Coercion.
_NO_MATCH: Any = object()


@dataclass
class _Fields:
    ctx: PipeContext
    trace: list[FieldTrace]
    record: int
    missing: list[str] = field(default_factory=list)

    def evaluate(self, specs: dict[str, FieldSpec], scope: Any, prefix: str) -> dict[str, Any]:
        return {name: self._field(f"{prefix}{name}", spec, scope) for name, spec in specs.items()}

    def _field(self, path: str, spec: FieldSpec, scope: Any) -> Any:
        error = None
        if spec.type == "object":
            value = self._object(path, spec, scope)
        elif spec.items is not None:
            value = self._items(path, spec.items, spec.pipe, scope)
        elif spec.each:
            value = self._loop(path, spec, scope)
        else:
            value, error = self._scalar(path, spec, scope)
        if value is _NO_MATCH:
            value = spec.default
        if value is None and spec.required:
            reason = f"required Field `{path}` is " + (f"invalid: {error}" if error else "missing")
            self._trace(path, [], reason)
            self.missing.append(reason)
        return value

    def _object(self, path: str, spec: FieldSpec, scope: Any) -> Any:
        values = self._run(path, spec.pipe, scope)
        return self.evaluate(spec.fields, values[0], f"{path}.") if values else _NO_MATCH

    def _items(self, path: str, items: FieldSpec, pipe: Pipe, scope: Any) -> Any:
        values = self._run(path, pipe, scope)
        if not values:
            return _NO_MATCH
        coerced = []
        for index, raw in enumerate(values):
            value, error = _coerced(raw, items.type)
            if error:
                self._trace(f"{path}[{index}]", [], error)
            coerced.append(value)
        return coerced

    def _loop(self, path: str, spec: FieldSpec, scope: Any) -> Any:
        nodes = self._run(path, spec.each, scope)
        if not nodes:
            return _NO_MATCH
        return [
            self.evaluate(spec.fields, node, f"{path}[{index}].")
            for index, node in enumerate(nodes)
        ]

    def _scalar(self, path: str, spec: FieldSpec, scope: Any) -> tuple[Any, str | None]:
        values, steps = run_pipe(spec.pipe, scope, self.ctx)
        value, error = _coerced(values[0], spec.type) if values else (_NO_MATCH, None)
        self._trace(path, steps, error)
        if value is None and error is None:  # JSON null is no value, same as no match
            return _NO_MATCH, None
        return value, error

    def _run(self, path: str, pipe: Pipe, scope: Any) -> list[Any]:
        values, steps = run_pipe(pipe, scope, self.ctx)
        self._trace(path, steps)
        return values

    def _trace(self, path: str, steps: list[StepTrace], error: str | None = None) -> None:
        self.trace.append(FieldTrace(path, self.record, steps, error))


# --- Coercion ---------------------------------------------------------------


class CoercionError(Exception):
    pass


def _coerced(value: Any, type_: str | None) -> tuple[Any, str | None]:
    try:
        return _coerce(value, type_), None
    except CoercionError as exc:
        return None, str(exc)


# Anchored to reject what float()/int() would otherwise tolerate: whitespace, "inf", "1_000".
_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?")
_INTEGER = re.compile(r"-?\d+")
_BOOLEANS = {"true": True, "false": False}


def _coerce(value: Any, type_: str | None) -> Any:
    """Strict: converts representations, never cleans up text (that's the Pipe's job)."""
    if isinstance(value, Selector):
        value = value.xpath("string()").get()
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    match type_:
        case _ if value is None:
            return None
        case None:
            return value
        case "string" if isinstance(value, str):
            return value
        case "string" if is_number:
            return str(value)
        case "number" if is_number or (isinstance(value, str) and _NUMBER.fullmatch(value)):
            return float(value)
        case "integer" if isinstance(value, int) and not isinstance(value, bool):
            return value
        case "integer" if isinstance(value, float) and value.is_integer():
            return int(value)
        case "integer" if isinstance(value, str) and _INTEGER.fullmatch(value):
            return int(value)
        case "boolean" if isinstance(value, bool):
            return value
        case "boolean" if isinstance(value, str) and value in _BOOLEANS:
            return _BOOLEANS[value]
    raise CoercionError(f"cannot coerce {value!r} to {type_}")
