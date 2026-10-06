import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fake_tracker import FakeTracker
from psycopg.types.json import Jsonb

from groceries_scraper.dispatch.collector import CollectorClient
from groceries_scraper.dispatch.store import DispatchStore

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DSN is None, reason="set TEST_DATABASE_URL to a disposable database"
)
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[DispatchStore]:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS dispatch CASCADE")
    store = DispatchStore(DSN)
    store.prepare()
    yield store


def enqueue(store: DispatchStore, count: int) -> None:
    with psycopg.connect(store.dsn) as connection:
        for index in reversed(range(count)):
            item_id = f"item-{index:03}"
            connection.execute(
                "INSERT INTO dispatch.outbox (id, kind, payload, created_at) VALUES (%s,%s,%s,%s)",
                (
                    item_id,
                    "heartbeat",
                    Jsonb(
                        {
                            "id": item_id,
                            "kind": "heartbeat",
                            "ref": "walnuts@default",
                            "last_checked_at": "2026-10-06T12:00:00Z",
                        }
                    ),
                    NOW + timedelta(seconds=index),
                ),
            )


def test_250_items_are_delivered_oldest_first_in_three_authenticated_batches(
    tracker: FakeTracker,
    store: DispatchStore,
) -> None:
    enqueue(store, 250)
    client = CollectorClient(tracker.url, store)

    assert client.deliver_changes(now=NOW) == 250

    assert [(method, path) for method, path, _, _ in tracker.requests] == [
        ("POST", "/api/collector/changes"),
        ("POST", "/api/collector/changes"),
        ("POST", "/api/collector/changes"),
    ]
    assert [len(data["items"]) for _, _, _, data in tracker.requests] == [100, 100, 50]
    assert tracker.requests[0][3]["items"][0]["id"] == "item-000"
    assert tracker.requests[-1][3]["items"][-1]["id"] == "item-249"
    for _, _, headers, _ in tracker.requests:
        assert headers["Cf-Access-Client-Id"] == "test-client-id"
        assert headers["Cf-Access-Client-Secret"] == "test-client-secret"
    assert store.pending_batch() == []


@pytest.mark.parametrize("failure_status", [503, 0])
def test_failed_second_batch_keeps_150_items_and_retries_after_backoff_on_the_next_tick(
    tracker: FakeTracker,
    store: DispatchStore,
    failure_status: int,
) -> None:
    enqueue(store, 250)
    tracker.responses = [
        (200, {}, {"acked": [f"item-{index:03}" for index in range(100)], "rejected": []}),
        (failure_status, {}, {"error": "unavailable"}),
    ]

    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW) == 100
    assert len(store.pending_batch(limit=1000)) == 150
    assert len(tracker.requests) == 2
    # A fresh client represents a fresh CronJob process; retry timing must survive it.
    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW + timedelta(seconds=30)) == 0
    assert len(tracker.requests) == 2
    assert (
        CollectorClient(tracker.url, store).deliver_changes(now=NOW + timedelta(minutes=5)) == 150
    )
    assert [len(data["items"]) for _, _, _, data in tracker.requests] == [100, 100, 100, 50]
    assert tracker.requests[2][3]["items"][0]["id"] == "item-100"
    assert store.pending_batch() == []


@pytest.mark.parametrize(
    "response",
    [
        {"acked": ["item-200"], "rejected": []},
        {"acked": [], "rejected": []},
        {"acked": [42], "rejected": []},
        "test-client-id test-client-secret",
    ],
)
def test_invalid_acknowledgements_keep_the_batch_pending_and_stop_delivery(
    tracker: FakeTracker,
    store: DispatchStore,
    response: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enqueue(store, 250)
    tracker.responses = [(200, {}, response)]

    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW) == 0

    assert len(tracker.requests) == 1
    assert len(store.pending_batch(limit=1000)) == 250
    assert "test-client-id" not in caplog.text
    assert "test-client-secret" not in caplog.text


def test_rejected_items_are_final_and_never_resent(
    tracker: FakeTracker, store: DispatchStore
) -> None:
    enqueue(store, 2)
    tracker.reject = {"item-000": "unknown ref"}
    client = CollectorClient(tracker.url, store)

    assert client.deliver_changes(now=NOW) == 2
    assert client.deliver_changes(now=NOW + timedelta(minutes=5)) == 0
    assert len(tracker.requests) == 1
    assert store.pending_batch() == []


def test_400_stops_the_tick_and_logs_loudly_without_leaking_credentials(
    tracker: FakeTracker,
    store: DispatchStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enqueue(store, 250)
    tracker.responses = [(400, {}, {"error": "test-client-id test-client-secret"})]

    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW) == 0

    assert len(tracker.requests) == 1
    assert len(store.pending_batch(limit=1000)) == 250
    assert any(
        record.levelno == logging.CRITICAL and "400" in record.message for record in caplog.records
    )
    assert "test-client-id" not in caplog.text
    assert "test-client-secret" not in caplog.text


def test_dry_run_validates_all_batches_without_finalising_or_backing_off_items(
    tracker: FakeTracker,
    store: DispatchStore,
) -> None:
    enqueue(store, 250)
    before = store.pending_batch(limit=1000)
    tracker.responses = [(200, {}, {"acked": [], "rejected": []})] * 3

    assert CollectorClient(tracker.url, store).deliver_changes(dry_run=True, now=NOW) == 0

    assert [path for _, path, _, _ in tracker.requests] == ["/api/collector/changes?dry_run=1"] * 3
    assert [len(data["items"]) for _, _, _, data in tracker.requests] == [100, 100, 50]
    assert store.pending_batch(limit=1000) == before
    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW) == 250


def test_watch_list_refresh_uses_etag_and_304_preserves_entries(
    tracker: FakeTracker,
    store: DispatchStore,
) -> None:
    entries = [
        {
            "ref": "walnuts@default",
            "url": "https://shop.example/walnuts",
            "location": None,
            "check_soon": True,
        }
    ]
    tracker.responses = [
        (200, {"ETag": '"42"'}, {"version": "42", "entries": entries}),
        (304, {}, None),
        (200, {"ETag": '"43"'}, {"version": "43", "entries": []}),
        (304, {}, None),
    ]
    assert CollectorClient(tracker.url, store).refresh_watch_list()
    assert store.watch_list() == ('"42"', entries)
    assert not CollectorClient(tracker.url, store).refresh_watch_list()
    assert store.watch_list() == ('"42"', entries)
    assert tracker.requests[1][2]["If-None-Match"] == '"42"'
    assert CollectorClient(tracker.url, store).refresh_watch_list()
    assert store.watch_list() == ('"43"', [])
    assert not CollectorClient(tracker.url, store).refresh_watch_list()
    assert tracker.requests[3][2]["If-None-Match"] == '"43"'
    assert all(path == "/api/collector/watch-list" for _, path, _, _ in tracker.requests)


def test_catalogue_contains_accepting_sites_and_is_only_sent_when_public_data_changes(
    tracker: FakeTracker,
    store: DispatchStore,
    tmp_path: Path,
) -> None:
    site = yaml.safe_load(Path("sites/aldi_picks.yaml").read_text())
    (tmp_path / "aldi_picks.yaml").write_text(yaml.safe_dump(site))
    (tmp_path / "aldi.yaml").write_text(Path("sites/aldi.yaml").read_text())
    store_site = {
        **site,
        "site": "stores",
        "schedule": {"every": "1h", "enabled": False},
        "locations": {"sydney": {"label": "Sydney G412", "service_point": "G412"}},
    }
    (tmp_path / "stores.yml").write_text(yaml.safe_dump(store_site))

    assert CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    assert tracker.requests[0][0:2] == ("PUT", "/api/collector/site-catalogue")
    assert tracker.requests[0][3] == {
        "sites": [
            {
                "site": "aldi_picks",
                "accepts": r"^https://www\.aldi\.com\.au/product/",
                "locations": [{"name": "default", "label": "default"}],
                "store_specific": False,
                "every_seconds": 1800,
                "enabled": True,
            },
            {
                "site": "stores",
                "accepts": r"^https://www\.aldi\.com\.au/product/",
                "locations": [{"name": "sydney", "label": "Sydney G412"}],
                "store_specific": True,
                "every_seconds": 3600,
                "enabled": False,
            },
        ]
    }
    assert not CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    # Session details and download policy aren't part of the public catalogue.
    store_site["locations"]["sydney"]["service_point"] = "G999"
    (tmp_path / "stores.yml").write_text(yaml.safe_dump(store_site))
    assert not CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    assert len(tracker.requests) == 1
    store_site["schedule"]["enabled"] = True
    (tmp_path / "stores.yml").write_text(yaml.safe_dump(store_site))
    assert CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    assert len(tracker.requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"version": "44", "entries": [{"ref": "missing-url"}]},
        {
            "version": "44",
            "entries": [
                {
                    "ref": "duplicate",
                    "url": "https://shop.example/one",
                    "location": None,
                    "check_soon": False,
                },
                {
                    "ref": "duplicate",
                    "url": "https://shop.example/two",
                    "location": None,
                    "check_soon": False,
                },
            ],
        },
        "test-client-id test-client-secret",
    ],
)
def test_invalid_watch_list_leaves_cached_entries_and_etag_untouched(
    tracker: FakeTracker,
    store: DispatchStore,
    response: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entries = [
        {
            "ref": "walnuts@default",
            "url": "https://shop.example/walnuts",
            "location": None,
            "check_soon": True,
        }
    ]
    store.replace_watch_list(entries, '"42"')
    tracker.responses = [(200, {"ETag": '"44"'}, response)]

    assert not CollectorClient(tracker.url, store).refresh_watch_list()

    assert store.watch_list() == ('"42"', entries)
    assert "test-client-id" not in caplog.text
    assert "test-client-secret" not in caplog.text


def test_redirects_do_not_forward_service_credentials(
    tracker: FakeTracker,
    store: DispatchStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker.responses = [(302, {"Location": tracker.url + "/login?test-client-secret"}, {})]

    assert not CollectorClient(tracker.url, store).refresh_watch_list()

    assert len(tracker.requests) == 1
    assert "test-client-secret" not in caplog.text


def test_repeated_delivery_failures_back_off_exponentially_and_cap_at_one_hour(
    tracker: FakeTracker,
    store: DispatchStore,
) -> None:
    enqueue(store, 1)
    tracker.responses = [(503, {}, {})] * 8
    retry_seconds = [0, 60, 180, 420, 900, 1860, 3780, 7380]

    for index, seconds in enumerate(retry_seconds):
        if seconds:
            assert (
                CollectorClient(tracker.url, store).deliver_changes(
                    now=NOW + timedelta(seconds=seconds - 1),
                )
                == 0
            )
            assert len(tracker.requests) == index
        assert (
            CollectorClient(tracker.url, store).deliver_changes(
                now=NOW + timedelta(seconds=seconds),
            )
            == 0
        )
        assert len(tracker.requests) == index + 1


def test_failed_catalogue_publish_is_retried_and_dry_run_failure_changes_no_delivery_state(
    tracker: FakeTracker,
    store: DispatchStore,
    tmp_path: Path,
) -> None:
    (tmp_path / "aldi_picks.yaml").write_text(Path("sites/aldi_picks.yaml").read_text())
    tracker.responses = [(503, {}, {})]
    assert not CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    assert CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    assert not CollectorClient(tracker.url, store).publish_site_catalogue(tmp_path)
    enqueue(store, 1)
    before = store.pending_batch()
    tracker.responses = [(503, {}, {})]
    assert CollectorClient(tracker.url, store).deliver_changes(dry_run=True, now=NOW) == 0
    assert store.pending_batch() == before
    assert CollectorClient(tracker.url, store).deliver_changes(now=NOW) == 1
