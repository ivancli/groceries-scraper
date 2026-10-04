import json
import re
from datetime import UTC, datetime
from pathlib import Path

from groceries_scraper.config import parse_site
from groceries_scraper.run import create_run
from groceries_scraper.run.recording import RunRecorder

DEFAULTS = {
    "download_delay": 0,
    "concurrent_requests_per_domain": 1,
    "obey_robots": True,
    "record_level": "all",
}


def test_run_directory_is_runs_site_run_id(tmp_path: Path) -> None:
    run = create_run(tmp_path, "example_grocer", datetime(2026, 10, 2, 9, 30, 5, tzinfo=UTC))

    assert re.fullmatch(r"20261002T093005Z-[0-9a-f]{6}", run.run_id)
    assert run.path == tmp_path / "example_grocer" / run.run_id
    assert run.path.is_dir()
    assert run.location == "default"


def test_a_runs_location_is_saved_in_its_manifest(tmp_path: Path) -> None:
    site = parse_site(
        {
            "site": "s",
            "locations": {"melb": {"postcode": "3000"}},
            "start": [{"url": "https://shop.example/", "page_type": "listing"}],
            "page_types": {"listing": {}},
        },
        DEFAULTS,
    )
    run = create_run(tmp_path, site.site, location="melb")

    RunRecorder(run, site)

    assert run.path == tmp_path / "s" / run.run_id
    assert json.loads((run.path / "run.json").read_text())["location"] == "melb"


def test_runs_started_in_the_same_second_get_distinct_ids(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, tzinfo=UTC)

    first, second = create_run(tmp_path, "s", now), create_run(tmp_path, "s", now)

    assert first.run_id != second.run_id


def test_run_manifest_persists_reloadable_effective_config_and_sha256_hash(tmp_path: Path) -> None:
    site = parse_site(
        {
            "site": "s",
            "records": {"product": {}},
            "start": [{"url": "https://shop.example/", "page_type": "product"}],
            "page_types": {
                "product": {
                    "record": "product",
                    "fields": {
                        "name": {"css": "h1::text", "type": "string"},
                    },
                }
            },
        },
        DEFAULTS,
    )
    run = create_run(tmp_path, site.site)
    RunRecorder(run, site)
    manifest = json.loads((run.path / "run.json").read_text())
    assert manifest == {
        "site": "s",
        "run_id": run.run_id,
        "location": "default",
        "config_hash": "2128aeafab9b9ce4ba8057e494eae54e6c5b5d59659a382eff39dee80164a190",
        "config": {
            "site": "s",
            "settings": {
                "download_delay": 0.0,
                "concurrent_requests_per_domain": 1,
                "obey_robots": True,
                "record_level": "all",
            },
            "records": {"product": {}},
            "start": [{"url": "https://shop.example/", "page_type": "product"}],
            "page_types": {
                "product": {
                    "record": "product",
                    "fields": {
                        "name": {"type": "string", "<pipe>": [{"css": "h1::text"}]},
                    },
                }
            },
        },
    }
    assert parse_site(manifest["config"], {}) == site
