import re
from datetime import UTC, datetime
from pathlib import Path

from groceries_scraper.run import create_run


def test_run_directory_is_runs_site_run_id(tmp_path: Path) -> None:
    run = create_run(tmp_path, "example_grocer", datetime(2026, 10, 2, 9, 30, 5, tzinfo=UTC))

    assert re.fullmatch(r"20261002T093005Z-[0-9a-f]{6}", run.run_id)
    assert run.path == tmp_path / "example_grocer" / run.run_id
    assert run.path.is_dir()


def test_runs_started_in_the_same_second_get_distinct_ids(tmp_path: Path) -> None:
    now = datetime(2026, 10, 2, tzinfo=UTC)

    first, second = create_run(tmp_path, "s", now), create_run(tmp_path, "s", now)

    assert first.run_id != second.run_id
