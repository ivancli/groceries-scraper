from pathlib import Path

import pytest
from site_fixtures import assert_site_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted(
    path
    for path in (ROOT / "tests" / "sites").glob("*")
    if path.is_dir() and not path.name.startswith(".") and path.name != "__pycache__"
)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda path: path.name)
def test_site_golden_fixture(fixture: Path, tmp_path: Path) -> None:
    assert_site_fixture(fixture, ROOT / "sites" / f"{fixture.name}.yaml", tmp_path)
