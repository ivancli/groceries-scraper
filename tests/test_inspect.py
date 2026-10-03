import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from groceries_scraper.cli import app


@pytest.fixture
def recorded_run(tmp_path: Path) -> Path:
    captures = tmp_path / "captures"
    traces = tmp_path / "traces"
    captures.mkdir()
    traces.mkdir()
    for number, page_type, parent in [(1, "listing", None), (2, "product", 1)]:
        meta = {
            "capture_no": number,
            "page_type": page_type,
            "parent_capture_no": parent,
            "variables": {"sku": "A1", "session": {}},
            "request": {
                "method": "GET",
                "url": f"https://shop.example/{page_type}",
                "headers": {"Authorization": "[REDACTED]"},
                "body": "",
                "body_encoding": "base64",
            },
            "response": {
                "url": f"https://shop.example/{page_type}",
                "status": 200,
                "headers": {"Content-Type": ["application/json; charset=utf-8"]},
                "timing": {"elapsed_seconds": 0.125},
            },
        }
        (captures / f"{number:04d}-{page_type}.meta.json").write_text(json.dumps(meta))
        (captures / f"{number:04d}-{page_type}.body").write_bytes(b'{"name":"Milk"}')
    trace: dict[str, Any] = {
        "capture_no": 2,
        "page_type": "product",
        "loop": None,
        "fields": [
            {
                "record": 0,
                "path": "name",
                "steps": [{"step": "jsonpath", "output": ["Milk"], "error": None}],
                "error": None,
            },
            {
                "record": 0,
                "path": "price",
                "steps": [{"step": "jsonpath", "output": ["$3.50"], "error": None}],
                "error": "cannot coerce '$3.50' to number",
            },
        ],
        "follow": [],
        "dropped": [],
    }
    (traces / "0002-product.trace.json").write_text(json.dumps(trace))
    return tmp_path


def inspect(run: Path, *args: str, color: bool = False) -> Result:
    return CliRunner().invoke(
        app,
        ["inspect", str(run), "2", *args],
        env={
            "COLUMNS": "100",
            "FORCE_COLOR": "1" if color else "",
            "TERM": "xterm",
            "NO_COLOR": None,
        },
        color=color,
    )


def test_inspect_shows_capture_trace_and_parent_chain(recorded_run: Path) -> None:
    result = inspect(recorded_run)

    assert result.exit_code == 0, result.output
    assert "GET https://shop.example/product" in result.stdout
    assert 'jsonpath -> ["Milk"]' in result.stdout
    assert "ERROR: cannot coerce '$3.50' to number" in result.stdout
    assert "1 (listing)" in result.stdout
    assert "2 (product)" in result.stdout


def test_inspect_rendering_snapshot(recorded_run: Path) -> None:
    result = inspect(recorded_run)

    assert result.exit_code == 0, result.output
    rendered = "\n".join(line.rstrip() for line in result.stdout.splitlines()) + "\n"
    assert rendered == (Path(__file__).parent / "fixtures" / "inspect.txt").read_text()


def test_inspect_pretty_prints_json_body_only_when_requested(recorded_run: Path) -> None:
    ordinary = inspect(recorded_run)
    with_body = inspect(recorded_run, "--body")

    assert ordinary.exit_code == with_body.exit_code == 0
    assert "Response body" not in ordinary.stdout
    assert 'Response body\n{\n  "name": "Milk"\n}\n' in with_body.stdout


def test_inspect_pretty_prints_html_body(recorded_run: Path) -> None:
    path = recorded_run / "captures" / "0002-product.meta.json"
    meta = json.loads(path.read_text())
    meta["response"]["headers"] = {"Content-Type": ["text/html; charset=iso-8859-1"]}
    path.write_text(json.dumps(meta))
    (recorded_run / "captures" / "0002-product.body").write_bytes(
        b"<html><body><h1>Cr\xe8me</h1><p>Milk</p></body></html>"
    )

    result = inspect(recorded_run, "--body")

    assert result.exit_code == 0, result.output
    assert "<h1>Crème</h1>\n<p>Milk</p>" in result.stdout


def test_inspect_decodes_compressed_response_body(recorded_run: Path) -> None:
    path = recorded_run / "captures" / "0002-product.meta.json"
    meta = json.loads(path.read_text())
    meta["response"]["headers"]["Content-Encoding"] = ["gzip"]
    path.write_text(json.dumps(meta))
    (recorded_run / "captures" / "0002-product.body").write_bytes(gzip.compress(b'{"name":"Milk"}'))

    result = inspect(recorded_run, "--body")

    assert result.exit_code == 0, result.output
    assert '{\n  "name": "Milk"\n}' in result.stdout


def test_inspect_filters_fields_and_includes_nested_paths(recorded_run: Path) -> None:
    trace_file = recorded_run / "traces" / "0002-product.trace.json"
    trace = json.loads(trace_file.read_text())
    for path in ["sizes", "sizes[0].price", "sizes_extra", "sizes[1].price"]:
        trace["fields"].append({"record": 0, "path": path, "steps": [], "error": None})
    trace_file.write_text(json.dumps(trace))

    result = inspect(recorded_run, "--field", "sizes")

    assert result.exit_code == 0, result.output
    assert "Field sizes (Record 0)" in result.stdout
    assert "Field sizes[0].price" in result.stdout
    assert "Field sizes[1].price" in result.stdout
    assert "Field sizes_extra" not in result.stdout
    assert "Field name" not in result.stdout
    assert "Field price" not in result.stdout


def test_inspect_shows_loop_follow_steps_and_dropped_records(recorded_run: Path) -> None:
    trace_file = recorded_run / "traces" / "0002-product.trace.json"
    trace = json.loads(trace_file.read_text())
    trace["loop"] = [{"step": "css", "output": ["<div>Milk</div>"], "error": None}]
    trace["follow"] = [
        {
            "rule": 1,
            "path": "pass.sku",
            "node": 2,
            "steps": [
                {"step": "css", "output": [" A1 "], "error": None},
                {"step": "strip", "output": ["A1"], "error": None},
                {"step": "fn", "output": [], "error": "Unknown function"},
            ],
            "error": "Follow failed",
        }
    ]
    trace["dropped"] = [{"index": 0, "reason": "required Field price is missing"}]
    trace_file.write_text(json.dumps(trace))

    result = inspect(recorded_run)

    assert result.exit_code == 0, result.output
    assert "Page Loop" in result.stdout
    assert "Follow Rule 1: pass.sku (Loop node 2)" in result.stdout
    assert '1. css -> [" A1 "]' in result.stdout
    assert '2. strip -> ["A1"]' in result.stdout
    assert "3. fn -> []" in result.stdout
    assert "ERROR: Unknown function" in result.stdout
    assert "ERROR: Follow failed" in result.stdout
    assert "Dropped Record 0" in result.stdout
    assert "ERROR: required Field price is missing" in result.stdout


def test_inspect_missing_parent_is_readable_for_errors_only_runs(recorded_run: Path) -> None:
    (recorded_run / "captures" / "0001-listing.meta.json").unlink()

    result = inspect(recorded_run)

    assert result.exit_code == 0, result.output
    assert "Parent chain: 1 (not recorded) -> 2 (product)" in result.stdout


def test_inspect_session_setup_and_empty_http_exchange_traces(recorded_run: Path) -> None:
    path = recorded_run / "traces" / "0002-product.trace.json"
    path.write_text(json.dumps({"capture_no": 2, "page_type": "product"}))
    empty = inspect(recorded_run)
    assert empty.exit_code == 0, empty.output
    assert "No extraction entries recorded." in empty.stdout
    path.write_text(
        json.dumps(
            {
                "fields": [{"path": "csrf", "steps": [{"step": "css", "output": ["[REDACTED]"]}]}],
                "error": "Session Setup failed",
            }
        )
    )

    setup = inspect(recorded_run)

    assert setup.exit_code == 0, setup.output
    assert "Field csrf" in setup.stdout
    assert "ERROR: Session Setup failed" in setup.stdout


@pytest.mark.parametrize("missing", ["capture", "trace", "body"])
def test_inspect_reports_missing_artifacts_without_a_traceback(
    recorded_run: Path, missing: str
) -> None:
    paths = {
        "capture": "captures/0002-product.meta.json",
        "trace": "traces/0002-product.trace.json",
        "body": "captures/0002-product.body",
    }
    (recorded_run / paths[missing]).unlink()

    result = inspect(recorded_run, "--body")

    assert result.exit_code == 1
    assert "Cannot inspect Capture 2:" in result.stderr
    assert "Traceback" not in result.output


def test_inspect_highlights_errors_on_color_terminals(recorded_run: Path) -> None:
    result = inspect(recorded_run, color=True)

    assert result.exit_code == 0, result.output
    assert "\x1b[1;31m" in result.stdout
    assert "ERROR: cannot coerce '$3.50' to number" in result.stdout


def test_inspect_reports_an_unknown_field_and_rejects_non_positive_capture_numbers(
    recorded_run: Path,
) -> None:
    missing_field = inspect(recorded_run, "--field", "unknown")
    invalid_number = CliRunner().invoke(app, ["inspect", str(recorded_run), "0"])

    assert missing_field.exit_code == 0
    assert "No trace for Field unknown" in missing_field.stdout
    assert invalid_number.exit_code == 2


def test_inspect_reports_invalid_json(recorded_run: Path) -> None:
    (recorded_run / "traces" / "0002-product.trace.json").write_text("{invalid")

    result = inspect(recorded_run)

    assert result.exit_code == 1
    assert "Cannot read" in result.stderr
    assert "0002-product.trace.json" in result.stderr


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_inspect_reports_corrupt_compressed_bodies(recorded_run: Path, encoding: str) -> None:
    path = recorded_run / "captures" / "0002-product.meta.json"
    meta = json.loads(path.read_text())
    meta["response"]["headers"]["Content-Encoding"] = [encoding]
    path.write_text(json.dumps(meta))
    (recorded_run / "captures" / "0002-product.body").write_bytes(b"broken compressed body")

    result = inspect(recorded_run, "--body")

    assert result.exit_code == 1
    assert "Cannot inspect Capture 2:" in result.stderr
