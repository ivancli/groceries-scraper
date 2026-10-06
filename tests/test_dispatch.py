import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fake_tracker import FakeTracker

from groceries_scraper.dispatch.collector import CollectorClient
from groceries_scraper.dispatch.dispatcher import Dispatcher, JobStatus
from groceries_scraper.dispatch.store import DispatchStore
from groceries_scraper.run.directory import SavedRun
from groceries_scraper.run.sinks.postgres import PostgresSink
from groceries_scraper.run.supply import write_jsonl

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
ROOT = Path(__file__).parent.parent
TEMPLATE = (ROOT / "tests" / "fixtures" / "job.yaml").read_text()
JOB_SINK = "postgresql://scrape@postgres/scrape"
T0 = datetime(2026, 10, 6, 12, tzinfo=UTC)
ALDI = "https://www.aldi.com.au/product/"
SHOP = "https://shop.example/p/"

SHOP_SITE: dict[str, Any] = {
    "site": "shop",
    "schedule": {"every": "1h"},
    "accepts": {"page_type": "product", "url": "^https://shop\\.example/p/", "examples": [SHOP]},
    "locations": {"north": {"label": "North"}, "south": {}},
    "records": {"product": {"key": ["url"]}},
    "start": [{"url": "https://shop.example/", "page_type": "product"}],
    "page_types": {
        "product": {
            "record": "product",
            "fields": {
                "url": {"css": "link::attr(href)", "type": "string", "required": True},
                "name": {"css": "h1::text", "type": "string", "required": True},
                "price": {"css": "b::text", "type": "number", "required": True},
            },
        }
    },
}


class FakeCluster:
    """Jobs run only when a test finishes them."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.config_maps: dict[str, dict[str, Any]] = {}
        self.statuses: dict[str, JobStatus] = {}
        self.refuse_jobs = False
        self.time_out_after_creating = False
        self.refuse_config_maps = False

    def create_job(self, job: dict[str, Any]) -> str:
        if self.refuse_jobs:
            raise OSError("API server unreachable")
        name = job["metadata"]["name"]
        assert name not in self.jobs
        self.jobs[name] = job
        self.statuses[name] = "running"
        if self.time_out_after_creating:
            raise TimeoutError("the API server answered too late")
        return f"uid-{name}"

    def create_config_map(self, config_map: dict[str, Any]) -> None:
        if self.refuse_config_maps:
            raise OSError("API server unreachable")
        self.config_maps[config_map["metadata"]["name"]] = config_map

    def job_status(self, name: str) -> JobStatus:
        return self.statuses.get(name, "missing")

    def created(self) -> list[dict[str, Any]]:
        return list(self.jobs.values())


@pytest.fixture
def store() -> Iterator[DispatchStore]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS dispatch CASCADE")
        connection.execute("DROP TABLE IF EXISTS scrape_outcomes, scrape_records, scrape_runs")
    PostgresSink(DSN).prepare()
    store = DispatchStore(DSN)
    store.prepare()
    yield store


@pytest.fixture
def sites(tmp_path: Path) -> Path:
    path = tmp_path / "sites"
    path.mkdir()
    (path / "aldi_picks.yaml").write_text((ROOT / "sites" / "aldi_picks.yaml").read_text())
    (path / "shop.yaml").write_text(yaml.safe_dump(SHOP_SITE))
    return path


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
def dispatcher(
    tracker: FakeTracker, store: DispatchStore, sites: Path, cluster: FakeCluster
) -> Dispatcher:
    return Dispatcher(
        store=store,
        collector=CollectorClient(tracker.url, store),
        cluster=cluster,
        sites=sites,
        job_template=TEMPLATE,
        sink_url=JOB_SINK,
    )


def entry(ref: str, url: str, location: str | None = None, check_soon: bool = False) -> Any:
    return {"ref": ref, "url": url, "location": location, "check_soon": check_soon}


def dispatched(job: dict[str, Any], cluster: FakeCluster) -> tuple[str, str, list[str]]:
    labels = job["metadata"]["labels"]
    supply = cluster.config_maps[job["metadata"]["name"]]["data"]["supply.jsonl"]
    refs = [json.loads(line)["ref"] for line in supply.splitlines()]
    return labels["groceries-scraper/site"], labels["groceries-scraper/location"], refs


def finish(
    tmp_path: Path,
    cluster: FakeCluster,
    at: datetime,
    outcome: str = "ok",
    price: float = 5.49,
) -> None:
    """Runs every running Job: its Run reports `outcome` for each ref and is exported."""
    for name, job in cluster.jobs.items():
        if cluster.statuses[name] != "running":
            continue
        site, location, refs = dispatched(job, cluster)
        args = job["spec"]["template"]["spec"]["containers"][0]["args"]
        run_id = args[args.index("--run-id") + 1]
        supply = cluster.config_maps[name]["data"]["supply.jsonl"].splitlines()
        urls = {line["ref"]: line["url"] for line in map(json.loads, supply)}
        path = tmp_path / "runs" / site / run_id
        (path / "records").mkdir(parents=True)
        manifest = {
            "site": site,
            "run_id": run_id,
            "location": location,
            "config": {"site": site, "records": {"product": {"key": ["url"]}}},
            "health": {"level": "ok" if outcome == "ok" else "failed", "breaches": []},
            "supplied": True,
            "supplied_refs": len(refs),
            "job": name,
        }
        (path / "run.json").write_text(json.dumps(manifest))
        write_jsonl(
            path / "outcomes.jsonl",
            [{"ref": ref, "url": urls[ref], "outcome": outcome} for ref in refs],
        )
        if outcome == "ok":
            write_jsonl(
                path / "records" / "product.jsonl",
                [
                    {
                        "url": urls[ref],
                        "name": ref,
                        "price": price,
                        "_meta": {"ref": ref, "scraped_at": at.isoformat()},
                    }
                    for ref in refs
                ],
            )
        PostgresSink(DSN or "").export(SavedRun.load(path))
        with psycopg.connect(DSN or "") as connection:
            # The Sink timestamps unsuccessful outcomes on export, in real time.
            connection.execute("UPDATE scrape_outcomes SET at = %s WHERE run_id = %s", (at, run_id))
        cluster.statuses[name] = "finished"


def test_a_tick_after_two_days_off_dispatches_every_entry_once_by_site_and_location(
    tmp_path: Path, tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [
        entry("walnuts", ALDI + "walnuts-1"),
        entry("brioche", ALDI + "brioche-2"),
        *(entry(f"n{i:03}", f"{SHOP}{i}", "north") for i in range(201)),
        entry("s1", SHOP + "s1", "south"),
    ]
    dispatcher.tick(T0)
    finish(tmp_path, cluster, T0 + timedelta(minutes=3))
    dispatcher.tick(T0 + timedelta(minutes=5))
    assert len(cluster.jobs) == 4  # nothing more was due after those Runs were ingested

    cluster.jobs.clear()
    dispatcher.tick(T0 + timedelta(days=2))

    batches = [dispatched(job, cluster) for job in cluster.created()]
    assert sorted((site, location, len(refs)) for site, location, refs in batches) == [
        ("aldi_picks", "default", 2),
        ("shop", "north", 1),
        ("shop", "north", 200),
        ("shop", "south", 1),
    ]
    refs = [ref for _, _, batch in batches for ref in batch]
    assert sorted(refs) == sorted(entry["ref"] for entry in tracker.watch_list)
    job = cluster.created()[0]
    container = job["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    assert args[args.index("--record") + 1] == "errors"
    assert args[args.index("--sink") + 1] == JOB_SINK
    config_map = cluster.config_maps[job["metadata"]["name"]]
    assert config_map["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job["metadata"]["name"],
            "uid": f"uid-{job['metadata']['name']}",
        }
    ]


def test_in_flight_refs_are_not_dispatched_again(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    dispatcher.tick(T0)
    assert len(cluster.jobs) == 1

    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1", check_soon=True)]
    for minutes in (5, 60, 24 * 60):
        dispatcher.tick(T0 + timedelta(minutes=minutes))

    assert len(cluster.jobs) == 1


def test_failures_back_off_doubling_from_every_up_to_a_day(
    tmp_path: Path, tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    at = T0
    dispatcher.tick(at)
    for wait in (30, 60, 120, 240, 480, 960, 1440, 1440):  # minutes; aldi_picks runs every 30m
        finish(tmp_path, cluster, at, outcome="failed")
        jobs = len(cluster.jobs)
        dispatcher.tick(at + timedelta(minutes=wait) - timedelta(seconds=1))
        assert len(cluster.jobs) == jobs, f"dispatched before {wait}m"
        at += timedelta(minutes=wait)
        dispatcher.tick(at)
        assert len(cluster.jobs) == jobs + 1, f"not dispatched after {wait}m"

    finish(tmp_path, cluster, at)
    dispatcher.tick(at + timedelta(minutes=29))
    assert len(cluster.jobs) == 9
    dispatcher.tick(at + timedelta(minutes=30))
    assert len(cluster.jobs) == 10  # success restores the Schedule


def test_check_soon_waits_for_min_every_since_the_last_attempt(
    tmp_path: Path, tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    dispatcher.tick(T0)
    finish(tmp_path, cluster, T0)

    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1", check_soon=True)]
    dispatcher.tick(T0 + timedelta(minutes=14))
    assert len(cluster.jobs) == 1
    dispatcher.tick(T0 + timedelta(minutes=15))  # defaults.yaml min_every
    assert len(cluster.jobs) == 2


def test_unroutable_entries_are_reported_once_and_disabled_sites_skipped(
    tracker: FakeTracker,
    sites: Path,
    dispatcher: Dispatcher,
    cluster: FakeCluster,
) -> None:
    paused = {**SHOP_SITE, "site": "paused", "schedule": {"every": "1h", "enabled": False}}
    paused["accepts"] = {**SHOP_SITE["accepts"], "url": "^https://paused\\.example/"}
    paused["accepts"]["examples"] = ["https://paused.example/"]
    (sites / "paused.yaml").write_text(yaml.safe_dump(paused))
    tracker.watch_list = [
        entry("elsewhere", "https://elsewhere.example/p/1"),
        entry("homeless", SHOP + "1"),
        entry("lost", SHOP + "2", "west"),
        entry("aldi-north", ALDI + "walnuts-1", "north"),
        entry("aldi-default", ALDI + "brioche-2", "default"),
        entry("paused", "https://paused.example/p/1", "north"),
    ]

    dispatcher.tick(T0)
    dispatcher.tick(T0 + timedelta(minutes=5))

    assert [dispatched(job, cluster) for job in cluster.created()] == [
        ("aldi_picks", "default", ["aldi-default"])
    ]
    # Enqueued by the first tick, delivered by the second.
    delivered = [
        item
        for _, path, _, data in tracker.requests
        if path == "/api/collector/changes"
        for item in data["items"]
    ]
    assert sorted((item["ref"], item["outcome"]) for item in delivered) == [
        ("aldi-north", "unknown_location"),
        ("elsewhere", "no_site"),
        ("homeless", "no_home_store"),
        ("lost", "unknown_location"),
    ]


def test_a_job_that_was_never_created_frees_its_refs_without_failing_them(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    dispatcher.tick(T0)
    (name,) = cluster.jobs
    del cluster.statuses[name]  # e.g. the tick was killed before creating it

    dispatcher.tick(T0 + timedelta(minutes=5))

    assert len(cluster.jobs) == 2


def test_a_job_created_despite_an_error_stays_in_flight(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    cluster.time_out_after_creating = True
    with pytest.raises(TimeoutError):
        dispatcher.tick(T0)

    cluster.time_out_after_creating = False
    dispatcher.tick(T0 + timedelta(minutes=5))
    assert len(cluster.jobs) == 1


def test_a_config_map_failure_does_not_stop_the_other_jobs(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1"), entry("s1", SHOP + "1", "south")]
    cluster.refuse_config_maps = True

    dispatcher.tick(T0)

    assert len(cluster.jobs) == 2


def test_a_job_that_ends_without_a_run_fails_its_refs_and_frees_them(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    dispatcher.tick(T0)
    (name,) = cluster.jobs
    cluster.statuses[name] = "finished"  # e.g. OOM-killed before exporting

    dispatcher.tick(T0 + timedelta(minutes=5))
    assert len(cluster.jobs) == 1  # failed once: backs off for `every`
    dispatcher.tick(T0 + timedelta(minutes=35))
    assert len(cluster.jobs) == 2


def test_a_job_that_could_not_be_created_is_retried_on_the_next_tick(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster
) -> None:
    tracker.watch_list = [entry("walnuts", ALDI + "walnuts-1")]
    cluster.refuse_jobs = True
    with pytest.raises(OSError):
        dispatcher.tick(T0)

    cluster.refuse_jobs = False
    dispatcher.tick(T0 + timedelta(minutes=5))
    assert len(cluster.jobs) == 1


def test_a_dry_run_validates_delivery_and_creates_nothing(
    tracker: FakeTracker, dispatcher: Dispatcher, cluster: FakeCluster, store: DispatchStore
) -> None:
    tracker.watch_list = [entry("elsewhere", "https://elsewhere.example/p/1")]
    dispatcher.tick(T0)  # enqueues the no_site outcome
    tracker.requests.clear()

    dispatcher.tick(T0 + timedelta(minutes=5), dry_run=True)

    assert [(method, path) for method, path, _, _ in tracker.requests] == [
        ("POST", "/api/collector/changes?dry_run=1")
    ]
    assert cluster.jobs == {}
    assert len(store.pending_batch()) == 1
