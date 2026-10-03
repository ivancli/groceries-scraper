"""Read and display a recorded Capture beside its Extraction Trace."""

import gzip
import json
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from groceries_scraper.engine.page import parse_scope


def inspect_capture(
    run_dir: Path,
    capture_no: int,
    console: Console,
    field: str | None = None,
    body: bool = False,
) -> None:
    path = _capture_path(run_dir, capture_no)
    meta = _read_json(path)
    stem = path.name.removesuffix(".meta.json")
    trace = _read_json(run_dir / "traces" / f"{stem}.trace.json")
    response_body = _format_body(path.with_name(f"{stem}.body"), meta) if body else None
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column(f"Capture {capture_no} ({meta['page_type']})", ratio=1)
    table.add_column("Extraction Trace", ratio=1)
    table.add_row(_capture_summary(meta), _trace_summary(trace, field))
    console.print(table)
    console.print(Text("Parent chain: " + _parent_chain(run_dir, meta)))
    if response_body is not None:
        console.print(Text("Response body", style="bold"))
        console.print(Text(response_body), soft_wrap=True)


def _format_body(path: Path, meta: dict[str, Any]) -> str:
    try:
        body = _decompress_body(path.read_bytes(), _header(meta, "Content-Encoding"))
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f"Cannot decode response body {path}: {exc}") from exc
    content_type = _header(meta, "Content-Type")
    try:
        return json.dumps(json.loads(body), ensure_ascii=False, indent=2)
    except (ValueError, UnicodeError):
        if "html" in content_type.lower():
            scope = parse_scope("html", body, content_type)
            output = BytesIO()
            scope.root.getroottree().write(
                output, encoding="utf-8", pretty_print=True, method="html"
            )
            return output.getvalue().decode("utf-8").rstrip("\n")
        # Failed JSON extraction should still leave the original body inspectable.
        return body.decode("utf-8", errors="replace")


def _decompress_body(body: bytes, content_encoding: str) -> bytes:
    encodings = content_encoding.lower().split(",")
    for encoding in reversed(encodings):
        match encoding.strip():
            case "gzip" | "x-gzip":
                body = gzip.decompress(body)
            case "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            case "" | "identity":
                pass
            case _:
                raise ValueError(f"Unsupported response Content-Encoding: {encoding.strip()}")
    return body


def _header(meta: dict[str, Any], name: str) -> str:
    for key, value in meta["response"].get("headers", {}).items():
        if key.lower() == name.lower():
            return ", ".join(value) if isinstance(value, list) else str(value)
    return ""


def _capture_path(run_dir: Path, capture_no: int) -> Path:
    paths = list((run_dir / "captures").glob(f"{capture_no:04d}-*.meta.json"))
    if not paths:
        raise ValueError(f"Capture {capture_no} is not recorded in {run_dir}")
    if len(paths) != 1:
        raise ValueError(f"Multiple files for Capture {capture_no} in {run_dir}")
    return paths[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _capture_summary(meta: dict[str, Any]) -> Text:
    text = Text()
    request, response = meta["request"], meta["response"]
    text.append(f"{request['method']} {request['url']}\n")
    status = response["status"]
    text.append(f"HTTP {status}\n", style="bold red" if not 200 <= status < 300 else "")
    if response.get("url") != request["url"]:
        text.append(f"Response URL: {response.get('url')}\n")
    timing = response.get("timing", {})
    if "elapsed_seconds" in timing:
        text.append(f"Elapsed: {timing['elapsed_seconds']:.3f}s\n")
    for name in ("started_at", "finished_at"):
        if name in timing:
            text.append(f"{name}: {timing[name]}\n")
    for label, data in [("Request headers", request), ("Response headers", response)]:
        text.append(f"\n{label}:\n", style="bold")
        for name, value in data.get("headers", {}).items():
            text.append(f"  {name}: {_value(value)}\n")
    if request.get("body"):
        text.append(f"\nRequest body ({request.get('body_encoding', 'text')}):\n", style="bold")
        text.append(f"{request['body']}\n")
    text.append("\nVariables:\n", style="bold")
    text.append(_value(meta.get("variables", {})))
    return text


def _error(text: Text, error: Any) -> None:
    if error:
        text.append(f"  ERROR: {error}\n", style="bold red")


def _steps(text: Text, steps: list[dict[str, Any]]) -> None:
    for number, step in enumerate(steps, 1):
        text.append(f"  {number}. {step['step']} -> {_value(step['output'])}\n")
        _error(text, step.get("error"))


def _matches_field(path: str, field: str) -> bool:
    return path == field or path.startswith((field + ".", field + "["))


def _trace_summary(trace: dict[str, Any], field: str | None) -> Text:
    text = Text()
    _error(text, trace.get("error"))
    if field is None and trace.get("loop") is not None:
        text.append("Page Loop\n", style="bold")
        _steps(text, trace["loop"])
    matched = False
    for entry in trace.get("fields", []):
        if field is not None and not _matches_field(entry["path"], field):
            continue
        matched = True
        record = f" (Record {entry['record']})" if "record" in entry else ""
        text.append(f"Field {entry['path']}{record}\n", style="bold")
        _steps(text, entry["steps"])
        _error(text, entry.get("error"))
    if field is not None and not matched:
        text.append(f"No trace for Field {field}\n")
    if field is None:
        for entry in trace.get("follow", []):
            node = f" (Loop node {entry['node']})" if entry.get("node") is not None else ""
            text.append(f"Follow Rule {entry['rule']}: {entry['path']}{node}\n", style="bold")
            _steps(text, entry["steps"])
            _error(text, entry.get("error"))
    for dropped in trace.get("dropped", []):
        text.append(f"Dropped Record {dropped['index']}\n", style="bold red")
        _error(text, dropped["reason"])
    if not text:
        text.append("No extraction entries recorded.")
    return text


def _parent_chain(run_dir: Path, meta: dict[str, Any]) -> str:
    chain: list[str] = []
    seen: set[int] = set()
    while True:
        number = meta["capture_no"]
        if number in seen:
            chain.append(f"{number} (cycle)")
            break
        seen.add(number)
        chain.append(f"{number} ({meta['page_type']})")
        parent = meta.get("parent_capture_no")
        if parent is None:
            break
        try:
            path = _capture_path(run_dir, parent)
        except ValueError:
            chain.append(f"{parent} (not recorded)")
            break
        meta = _read_json(path)
    return " -> ".join(reversed(chain))
