import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner, Result

from groceries_scraper.cli import app
from groceries_scraper.coordination import Admitted, Coordinator, Permit, Policy, Wait

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROLE = "scrape_coordinator_test"
PASSWORD = "coordinator-test"
LEASE = timedelta(minutes=5)


def _site(name: str, retailer: str, delay: float, concurrency: int) -> str:
    return f"""
site: {name}
settings: {{download_delay: {delay}, concurrent_requests_per_domain: {concurrency}}}
schedule: {{every: 30m}}
accepts:
  page_type: product
  retailer: {{key: {retailer}, name: {retailer.title()}}}
  url: '^https://{retailer}\\.example/p/'
  examples: ['https://{retailer}.example/p/1']
records: {{product: {{key: [url]}}}}
start: [{{url: 'https://{retailer}.example/p/1', page_type: product}}]
page_types:
  product:
    record: product
    fields:
      url: {{xpath: "//link/@href", type: string, required: true}}
      name: {{xpath: "//h1/text()", type: string, required: true}}
      price: {{xpath: "//b/text()", type: number, required: true}}
"""


@pytest.fixture
def sites(tmp_path: Path) -> Path:
    for name, retailer, delay, concurrency in [
        ("shop_picks", "shop", 0.5, 4),
        ("shop_slow_picks", "shop", 2.0, 2),
        ("other_picks", "other", 1.0, 3),
    ]:
        (tmp_path / f"{name}.yaml").write_text(_site(name, retailer, delay, concurrency))
    return tmp_path


def _invoke(*args: str) -> Result:
    assert DSN is not None
    env = {"SCRAPE_SINK": DSN, "SCRAPE_COORDINATOR_PASSWORD": PASSWORD}
    return CliRunner().invoke(app, ["coordinator", *args], env=env)


@pytest.fixture
def coordinator(sites: Path) -> Coordinator:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS coordination CASCADE")
    result = _invoke("setup", "--role", ROLE, "--sites", str(sites))
    assert result.exit_code == 0, result.output
    return Coordinator(make_conninfo(DSN, user=ROLE, password=PASSWORD))


def test_setup_holds_each_retailer_to_its_strictest_site(coordinator: Coordinator) -> None:
    loose = Policy(min_interval=0, max_concurrent=100)
    first = coordinator.acquire_permit("shop", "search", loose, lease=LEASE)
    assert isinstance(first, Permit)
    coordinator.acquire_permit("shop", "search", loose, lease=LEASE, paced=False)

    # shop_slow_picks: two in flight, two seconds apart.
    assert coordinator.acquire_permit("shop", "search", loose, lease=LEASE, paced=False) == Wait(
        None
    )
    coordinator.release_permit(first)
    waiting = coordinator.acquire_permit("shop", "search", loose, lease=LEASE)
    assert isinstance(waiting, Wait) and waiting.until is not None
    assert waiting.until - first.granted_at == timedelta(seconds=2)


def test_status_shows_the_execution_holding_the_search_slot(coordinator: Coordinator) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    coordinator.admit_search("e1", "coles:milk", "api-pod-abc", deadline)

    result = _invoke("status")

    assert result.exit_code == 0, result.output
    assert "e1" in result.output and "api-pod-abc" in result.output


def test_recovery_needs_the_operator_to_confirm_the_owner_is_terminated(
    coordinator: Coordinator,
) -> None:
    search = coordinator.admit_search("e1", "w", "api-pod-abc", datetime.now(UTC) + LEASE)
    assert isinstance(search, Admitted)
    fence = str(search.fence)

    unconfirmed = _invoke("recover", "e1", "--fence", fence, "--owner", "api-pod-abc")
    wrong_owner = _invoke(
        "recover", "e1", "--fence", fence, "--owner", "api-pod-xyz", "--owner-terminated"
    )
    assert unconfirmed.exit_code != 0 and wrong_owner.exit_code != 0
    assert coordinator.admit_search("e2", "w", "api-pod-def", datetime.now(UTC) + LEASE) == "busy"

    recovered = _invoke(
        "recover", "e1", "--fence", fence, "--owner", "api-pod-abc", "--owner-terminated"
    )

    assert recovered.exit_code == 0, recovered.output
    after = coordinator.admit_search("e2", "w", "api-pod-def", datetime.now(UTC) + LEASE)
    assert isinstance(after, Admitted) and after.fence > search.fence
    # The recovered execution's own late cleanup report changes nothing.
    assert coordinator.end_search(search) is False
    assert coordinator.admit_search("e1", "w", "api-pod-abc", datetime.now(UTC) + LEASE) == (
        "replayed"
    )
