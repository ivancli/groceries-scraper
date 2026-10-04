import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from scrapy import Request
from site_fixtures import assert_site_fixture
from typer.testing import CliRunner

from groceries_scraper.adapter.fingerprint import request_fingerprint
from groceries_scraper.cli import app
from groceries_scraper.run.fixtures import FixtureError, record_data, save_fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "captures").mkdir(parents=True)
    (source / "records").mkdir()
    url = "https://shop.example/"
    config = {
        "site": "shop",
        "settings": {"record_level": "all"},
        "records": {"product": {}},
        "start": [{"url": url, "page_type": "listing"}],
        "page_types": {"listing": {"record": "product", "fields": {"name": {"css": "h1::text"}}}},
    }
    manifest = {
        "site": "shop",
        "run_id": "original",
        "config": config,
        "stats": {"finish_reason": "finished"},
        "health": {"level": "ok"},
    }
    (source / "run.json").write_text(json.dumps(manifest))
    meta = {
        "capture_no": 1,
        "request": {"fingerprint": request_fingerprint(Request(url), frozenset())},
        "response": {"status": 200, "headers": {"Content-Type": ["text/html"]}},
    }
    (source / "captures" / "0001-listing.meta.json").write_text(json.dumps(meta))
    (source / "captures" / "0001-listing.body").write_text("<h1>Milk</h1><p>Title</p>")
    _write_records(
        source,
        [
            {
                "name": "Milk",
                "_meta": {
                    "run_id": "original",
                    "site": "shop",
                    "record_type": "product",
                    "source_url": url,
                },
            }
        ],
    )
    return source


def _write_records(source: Path, rows: list[dict[str, Any]]) -> None:
    (source / "records" / "product.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )


def test_saved_fixture_replays_offline_and_selector_changes_show_a_record_diff(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    saved = runner.invoke(app, ["fixture", "save", str(source)])
    assert saved.exit_code == 0, saved.output
    fixture = tmp_path / "tests/sites/shop"
    manifest = json.loads((source / "run.json").read_text())
    config = tmp_path / "site.yaml"
    config.write_text(yaml.safe_dump(manifest["config"]))
    original = tmp_path / "original"
    original.mkdir()
    assert_site_fixture(fixture, config, original)

    manifest["config"]["page_types"]["listing"]["fields"]["name"]["css"] = "p::text"
    config.write_text(yaml.safe_dump(manifest["config"]))
    changed = tmp_path / "changed"
    changed.mkdir()
    with pytest.raises(AssertionError, match="replayed Records") as error:
        assert_site_fixture(fixture, config, changed)
    assert "Milk" in str(error.value) and "Title" in str(error.value)
    changed_lines = [
        line
        for line in str(error.value).splitlines()
        if line.startswith(("-", "+")) and not line.startswith(("---", "+++"))
    ]
    assert all("Milk" in line or "Title" in line for line in changed_lines)

    [edited_run] = (changed / "runs/shop").iterdir()
    saved = runner.invoke(app, ["fixture", "save", str(edited_run)])
    assert saved.exit_code == 0, saved.output
    [expected] = (fixture / "records/product.jsonl").read_text().splitlines()
    assert json.loads(expected)["name"] == "Title"
    updated = tmp_path / "updated"
    updated.mkdir()
    assert_site_fixture(fixture, config, updated)


def test_resaving_replaces_expected_records_and_stale_capture_files(
    source: Path, tmp_path: Path
) -> None:
    fixture = save_fixture(source, tmp_path / "fixtures")
    (fixture / "captures" / "stale.body").write_text("stale")
    _write_records(source, [{"name": "Updated", "_meta": {"run_id": "new"}}])
    save_fixture(source, tmp_path / "fixtures")
    assert record_data(fixture / "records") == {"product.jsonl": [{"name": "Updated"}]}
    assert not (fixture / "captures" / "stale.body").exists()


@pytest.mark.parametrize("damage", ["limit", "incomplete", "errors", "body", "site", "records"])
def test_invalid_source_leaves_existing_fixture_intact(
    source: Path, tmp_path: Path, damage: str
) -> None:
    fixture = save_fixture(source, tmp_path / "fixtures")
    before = (fixture / "records" / "product.jsonl").read_bytes()
    manifest = json.loads((source / "run.json").read_text())
    if damage == "limit":
        manifest["stats"]["finish_reason"] = "closespider_itemcount"
    elif damage == "incomplete":
        del manifest["stats"]
    elif damage == "errors":
        manifest["config"]["settings"]["record_level"] = "errors"
    elif damage == "body":
        (source / "captures" / "0001-listing.body").unlink()
    elif damage == "site":
        manifest["site"] = "../outside"
    elif damage == "records":
        _write_records(source, [])
    (source / "run.json").write_text(json.dumps(manifest))
    with pytest.raises(FixtureError):
        save_fixture(source, tmp_path / "fixtures")
    assert (fixture / "records" / "product.jsonl").read_bytes() == before


def test_fixture_cli_saves_and_reports_invalid_runs(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["fixture", "save", str(source)])
    assert result.exit_code == 0 and "tests/sites/shop" in result.stdout
    result = CliRunner().invoke(app, ["fixture", "save", str(tmp_path / "absent")])
    assert result.exit_code == 1 and "not a Run directory" in result.stderr


def test_fixture_cli_strips_only_volatile_record_metadata(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_records(
        source,
        [
            {
                "name": "Milk",
                "_meta": {
                    "site": "shop",
                    "record_type": "product",
                    "source_url": "https://shop.example/",
                    "run_id": "original",
                    "scraped_at": "2026-10-03T00:00:00Z",
                    "capture_no": 42,
                },
            }
        ],
    )
    result = CliRunner().invoke(app, ["fixture", "save", str(source)])
    assert result.exit_code == 0, result.output
    rows = [
        json.loads(line)
        for line in (tmp_path / "tests/sites/shop/records/product.jsonl").read_text().splitlines()
    ]
    assert rows == [
        {
            "name": "Milk",
            "_meta": {
                "site": "shop",
                "record_type": "product",
                "source_url": "https://shop.example/",
                "location": "default",  # a Run from before Locations scraped the implicit one
            },
        }
    ]


def test_record_comparison_ignores_run_order_but_preserves_duplicates_and_arrays(
    source: Path,
) -> None:
    _write_records(source, [{"name": "B"}, {"name": "A", "sizes": [2, 1]}, {"name": "B"}])
    assert record_data(source / "records") == {
        "product.jsonl": [{"name": "A", "sizes": [2, 1]}, {"name": "B"}, {"name": "B"}]
    }


@pytest.mark.parametrize("record", [[], None, {"name": "Milk", "_meta": []}])
def test_fixture_cli_reports_malformed_records_and_preserves_the_previous_fixture(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["fixture", "save", str(source)]).exit_code == 0
    expected = tmp_path / "tests/sites/shop/records/product.jsonl"
    previous = expected.read_bytes()
    (source / "records/product.jsonl").write_text(json.dumps(record) + "\n")

    result = runner.invoke(app, ["fixture", "save", str(source)])

    assert result.exit_code == 1
    assert "Cannot save fixture:" in result.stderr
    assert "product.jsonl:1" in result.stderr
    assert expected.read_bytes() == previous


def test_golden_suite_rejects_a_fixture_missing_its_manifest(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["fixture", "save", str(source)]).exit_code == 0
    (tmp_path / "tests/sites/shop/run.json").unlink()
    shutil.copy2(ROOT / "tests/test_sites.py", tmp_path / "tests/test_sites.py")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_sites.py"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "tests"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a Run directory" in result.stdout


@pytest.mark.parametrize("section", ["stats", "health"])
def test_fixture_cli_reports_malformed_manifest_sections(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = json.loads((source / "run.json").read_text())
    manifest[section] = None
    (source / "run.json").write_text(json.dumps(manifest))

    result = CliRunner().invoke(app, ["fixture", "save", str(source)])

    assert result.exit_code == 1
    assert "Cannot save fixture:" in result.stderr
    assert "run.json" in result.stderr and section in result.stderr


def test_resaving_through_cli_preserves_fixture_notes(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["fixture", "save", str(source)]).exit_code == 0
    notes = tmp_path / "tests/sites/shop/README.md"
    notes.write_text("This fixture covers a public catalogue response.\n")

    result = runner.invoke(app, ["fixture", "save", str(source)])

    assert result.exit_code == 0, result.output
    assert notes.read_text() == "This fixture covers a public catalogue response.\n"


@pytest.mark.parametrize(
    "attempt",
    [
        "socket.socket().connect(('127.0.0.1', 9))",
        "socket.socket().connect_ex(('127.0.0.1', 9))",
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'ping', ('127.0.0.1', 9))",
        "socket.getaddrinfo('127.0.0.1', 9)",
    ],
)
def test_golden_replay_refuses_and_reports_network_attempts(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempt: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["fixture", "save", str(source)]).exit_code == 0
    fixture = tmp_path / "tests/sites/shop"
    output = tmp_path / "offline"
    output.mkdir()
    (output / "attempt_network.py").write_text(
        f"import socket\ndef run(value, ctx):\n    {attempt}\n    return value\n"
    )
    config = json.loads((source / "run.json").read_text())["config"]
    config["page_types"]["listing"]["fields"]["name"] = [
        {"css": "h1::text"},
        {"fn": "attempt_network:run"},
    ]
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump(config))

    with pytest.raises(AssertionError, match="attempted network access"):
        assert_site_fixture(fixture, site, output)
