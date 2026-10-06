"""Transactional price history and outbox for Runs registered by the Dispatcher."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SYDNEY = ZoneInfo("Australia/Sydney")
SCHEMA_LOCK = 0xD15A7C
TRACKER_OUTCOMES = {"no_site", "no_home_store", "unknown_location", "not_found", "blocked"}
STATE_FIELDS = (
    "shelf_price_cents",
    "regular_price_cents",
    "is_deal",
    "unit_price_cents",
    "unit_basis",
    "unit_price_text",
    "price_kind",
    "availability",
    "store_verified",
    "promo_text",
)


@dataclass(frozen=True)
class PendingItem:
    id: str
    payload: dict[str, Any]
    next_attempt_at: datetime | None


@dataclass(frozen=True)
class Dispatched:
    site: str
    run_id: str
    location: str
    job_name: str
    refs: list[str]


@dataclass(frozen=True)
class RefState:
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class DispatchStore:
    dsn: str = field(repr=False)

    def prepare(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute(Path(__file__).with_name("schema.sql").read_text())

    def pending_batch(self, *, limit: int = 100, offset: int = 0) -> list[PendingItem]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, payload, next_attempt_at FROM dispatch.outbox WHERE sent_at IS NULL"
                " ORDER BY created_at, id LIMIT %s OFFSET %s",
                (limit, offset),
            ).fetchall()
            return [PendingItem(**row) for row in rows]

    def finish_delivery(self, acked: list[str], rejected: dict[str, str], at: datetime) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "UPDATE dispatch.outbox SET sent_at = %s, attempts = attempts + 1,"
                " last_error = NULL, next_attempt_at = NULL"
                " WHERE id = ANY(%s) AND sent_at IS NULL",
                (at, acked),
            )
            for item_id, reason in rejected.items():
                connection.execute(
                    "UPDATE dispatch.outbox SET sent_at = %s, attempts = attempts + 1,"
                    " last_error = %s, next_attempt_at = NULL"
                    " WHERE id = %s AND sent_at IS NULL",
                    (at, reason, item_id),
                )

    def fail_delivery(self, ids: list[str], error: str, at: datetime) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "UPDATE dispatch.outbox SET attempts = attempts + 1, last_error = %s,"
                " next_attempt_at = %s + interval '1 second' *"
                " least(3600, 60 * power(2, least(attempts, 6)))"
                " WHERE id = ANY(%s) AND sent_at IS NULL",
                (error, at, ids),
            )

    def collector_state(self, key: str) -> str | None:
        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT value FROM dispatch.collector_state WHERE key = %s",
                (key,),
            ).fetchone()
            return row[0] if row is not None else None

    def save_collector_state(self, key: str, value: str) -> None:
        with psycopg.connect(self.dsn) as connection:
            _save_collector_state(connection, key, value)

    def watch_list(self) -> tuple[str | None, list[dict[str, Any]]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT value FROM dispatch.collector_state WHERE key = 'watch_etag'",
            ).fetchone()
            entries = connection.execute(
                "SELECT ref, url, location, check_soon FROM dispatch.watch_entries ORDER BY ref",
            ).fetchall()
            return (row["value"] if row is not None else None), entries

    def replace_watch_list(self, entries: list[dict[str, Any]], etag: str) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("DELETE FROM dispatch.watch_entries")
            for entry in entries:
                connection.execute(
                    "INSERT INTO dispatch.watch_entries (ref, url, location, check_soon, etag)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (entry["ref"], entry["url"], entry["location"], entry["check_soon"], etag),
                )
            _save_collector_state(connection, "watch_etag", etag)

    def record_dispatch(
        self,
        *,
        site: str,
        location: str,
        run_id: str,
        job_name: str,
        refs: list[str],
        created_at: datetime,
    ) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "INSERT INTO dispatch.dispatches"
                " (site, location, run_id, job_name, refs, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (site, location, run_id, job_name, refs, created_at),
            )

    def cancel_dispatch(self, site: str, run_id: str) -> None:
        """For a Job that was never created."""
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "DELETE FROM dispatch.dispatches"
                " WHERE site = %s AND run_id = %s AND ingested_at IS NULL",
                (site, run_id),
            )

    def in_flight(self) -> list[Dispatched]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT site, run_id, location, job_name, refs FROM dispatch.dispatches"
                " WHERE ingested_at IS NULL ORDER BY created_at, job_name"
            ).fetchall()
            return [Dispatched(**row) for row in rows]

    def ref_states(self) -> dict[str, RefState]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT ref, last_attempt_at, last_success_at, consecutive_failures"
                " FROM dispatch.latest_state"
            ).fetchall()
            return {row.pop("ref"): RefState(**row) for row in rows}

    def report_outcome(
        self, site: str | None, location: str | None, ref: str, outcome: str, at: datetime
    ) -> None:
        """An entry the Dispatcher could not route; reported once per distinct outcome."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            _report_outcome(connection, site, location, ref, outcome, at)

    def abandon(self, site: str, run_id: str, error: str, at: datetime) -> bool:
        """A Job that ended without an ingestable Run fails each of its refs at `at`."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            dispatched = connection.execute(
                "SELECT * FROM dispatch.dispatches WHERE site = %s AND run_id = %s FOR UPDATE",
                (site, run_id),
            ).fetchone()
            if dispatched is None or dispatched["ingested_at"] is not None:
                return False
            for ref in dispatched["refs"]:
                outcome = {"ref": ref, "outcome": "failed", "error": error, "at": at}
                self._check(connection, dispatched, outcome, None)
            connection.execute(
                "UPDATE dispatch.dispatches SET finished_at = %s, ingested_at = now()"
                " WHERE site = %s AND run_id = %s",
                (at, site, run_id),
            )
            return True

    def ingest(self, site: str, run_id: str) -> bool:
        """Returns false until exported, or for an unregistered/already ingested Run."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            dispatched = connection.execute(
                "SELECT * FROM dispatch.dispatches WHERE site = %s AND run_id = %s FOR UPDATE",
                (site, run_id),
            ).fetchone()
            if dispatched is None or dispatched["ingested_at"] is not None:
                return False
            # Match the Sink's lock so a re-export cannot replace rows between our reads.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{site}/{run_id}",))
            run = connection.execute(
                "SELECT * FROM scrape_runs WHERE site = %s AND run_id = %s", (site, run_id)
            ).fetchone()
            if (
                run is None
                or not run["manifest"].get("supplied")
                or run["manifest"].get("replay_of")
                or run["manifest"].get("health", {}).get("level") is None
            ):
                return False
            if run["location"] != dispatched["location"]:
                raise ValueError("Run Location does not match its dispatch")
            outcomes = connection.execute(
                "SELECT * FROM scrape_outcomes WHERE site = %s AND run_id = %s ORDER BY ref",
                (site, run_id),
            ).fetchall()
            if {outcome["ref"] for outcome in outcomes} != set(dispatched["refs"]):
                raise ValueError("Start Request Outcomes do not match the dispatched refs")
            records = connection.execute(
                "SELECT data, meta FROM scrape_records WHERE site = %s AND run_id = %s",
                (site, run_id),
            ).fetchall()
            by_ref = {row["meta"].get("ref"): row for row in records}
            for outcome in outcomes:
                self._check(connection, dispatched, outcome, by_ref.get(outcome["ref"]))
            connection.execute(
                "UPDATE dispatch.dispatches SET finished_at = %s, ingested_at = now()"
                " WHERE site = %s AND run_id = %s",
                (run["exported_at"], site, run_id),
            )
            return True

    def _check(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        dispatched: dict[str, Any],
        outcome: dict[str, Any],
        record: dict[str, Any] | None,
    ) -> None:
        site, location, run_id = dispatched["site"], dispatched["location"], dispatched["run_id"]
        ref = outcome["ref"]
        at, result, error = outcome["at"], outcome["outcome"], outcome["error"]
        state = None
        product: dict[str, Any] = {}
        if result == "ok":
            try:
                if record is None:
                    raise ValueError("ok outcome has no price Record")
                data, meta = record["data"], record["meta"]
                if data.get("currency", "AUD") not in (None, "AUD"):
                    raise ValueError("price currency must be AUD")
                observed_at = datetime.fromisoformat(meta["scraped_at"])
                if observed_at.tzinfo is None:
                    raise ValueError("scraped_at must include a timezone")
                at = observed_at
                state = _product_state(data)
                product = {
                    "name": data["name"],
                    "brand": data.get("brand"),
                    "size_text": data.get("size"),
                    "product_url": data["url"],
                }
            except (ValueError, KeyError, TypeError) as exc:
                result, error, state = "failed", str(exc), None
        connection.execute(
            "INSERT INTO dispatch.checks (site, location, ref, run_id, outcome, error, at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (site, location, ref, run_id, result, error, at),
        )
        if state is not None:
            assert record is not None
            columns = [
                "site",
                "location",
                "ref",
                "run_id",
                "capture_no",
                "observed_at",
                *STATE_FIELDS,
                *product,
            ]
            values = [
                site,
                location,
                ref,
                run_id,
                record["meta"].get("capture_no"),
                at,
                *(state[name] for name in STATE_FIELDS),
                *product.values(),
            ]
            connection.execute(
                f"INSERT INTO dispatch.price_observations ({', '.join(columns)})"
                f" VALUES ({', '.join(['%s'] * len(values))})",
                values,
            )
        connection.execute(
            "INSERT INTO dispatch.latest_state (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,)
        )
        latest = connection.execute(
            "SELECT * FROM dispatch.latest_state WHERE ref = %s FOR UPDATE", (ref,)
        ).fetchone()
        assert latest is not None
        health = "ok" if state is not None else "failing"
        since = (latest["failing_since"] or at) if health == "failing" else None
        newest_attempt = latest["last_attempt_at"] is None or at >= latest["last_attempt_at"]
        if (
            newest_attempt
            and latest["health"] != health
            and (latest["health"] is not None or health == "failing")
        ):
            _enqueue(
                connection,
                "health",
                site,
                location,
                ref,
                at,
                {
                    "at": _iso(at),
                    "health": health,
                    "since": _iso(since) if since else None,
                    "error": error,
                },
            )
        if state is not None:
            self._heartbeat(connection, site, location, ref, at)
        if state is not None and (
            latest["last_success_at"] is None or at >= latest["last_success_at"]
        ):
            state_hash = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
            if latest["state_hash"] != state_hash:
                _enqueue(
                    connection,
                    "change",
                    site,
                    location,
                    ref,
                    at,
                    {"observed_at": _iso(at), "state": state, "product": product},
                )
            connection.execute(
                "UPDATE dispatch.latest_state"
                " SET state_hash = %s, last_success_at = %s WHERE ref = %s",
                (state_hash, at, ref),
            )
        elif result in TRACKER_OUTCOMES:
            _report_outcome(connection, site, location, ref, result, at)
        if newest_attempt:
            connection.execute(
                "UPDATE dispatch.latest_state SET last_attempt_at = %s, health = %s,"
                " failing_since = %s, consecutive_failures = %s WHERE ref = %s",
                (
                    at,
                    health,
                    since,
                    latest["consecutive_failures"] + 1 if state is None else 0,
                    ref,
                ),
            )

    def _heartbeat(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        site: str,
        location: str,
        ref: str,
        at: datetime,
    ) -> None:
        today = at.astimezone(SYDNEY).date()
        start = datetime.combine(today, datetime.min.time(), SYDNEY)
        end = datetime.combine(today + timedelta(days=1), datetime.min.time(), SYDNEY)
        sent_today = connection.execute(
            "SELECT 1 FROM dispatch.outbox WHERE kind = 'heartbeat' AND payload->>'ref' = %s"
            " AND created_at >= %s AND created_at < %s LIMIT 1",
            (ref, start, end),
        ).fetchone()
        watch = connection.execute(
            "SELECT check_soon FROM dispatch.watch_entries WHERE ref = %s", (ref,)
        ).fetchone()
        if sent_today is None or (watch is not None and watch["check_soon"]):
            _enqueue(
                connection, "heartbeat", site, location, ref, at, {"last_checked_at": _iso(at)}
            )
        connection.execute(
            "UPDATE dispatch.latest_state"
            " SET last_heartbeat_date = greatest(last_heartbeat_date, %s) WHERE ref = %s",
            (today, ref),
        )


def _report_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    site: str | None,
    location: str | None,
    ref: str,
    outcome: str,
    at: datetime,
) -> None:
    connection.execute(
        "INSERT INTO dispatch.latest_state (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,)
    )
    reported = connection.execute(
        "SELECT reported_outcomes FROM dispatch.latest_state WHERE ref = %s FOR UPDATE", (ref,)
    ).fetchone()
    assert reported is not None
    if outcome in reported["reported_outcomes"]:
        return
    _enqueue(connection, "outcome", site, location, ref, at, {"at": _iso(at), "outcome": outcome})
    connection.execute(
        "UPDATE dispatch.latest_state"
        " SET reported_outcomes = array_append(reported_outcomes, %s) WHERE ref = %s",
        (outcome, ref),
    )


def _save_collector_state(connection: psycopg.Connection[Any], key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO dispatch.collector_state (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def _product_state(data: dict[str, Any]) -> dict[str, Any]:
    price = _cents(data["price"])
    if price is None:
        raise ValueError("price is required")
    return {
        "shelf_price_cents": price,
        "regular_price_cents": _cents(data.get("regular_price")),
        "unit_price_cents": _cents(data.get("unit_price")),
        **{name: data.get(name) for name in STATE_FIELDS if not name.endswith("_cents")},
    }


def _cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("price must be finite and non-negative")
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("price must be a dollar amount") from exc


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _enqueue(
    connection: psycopg.Connection[dict[str, Any]],
    kind: str,
    site: str | None,
    location: str | None,
    ref: str,
    at: datetime,
    payload: dict[str, Any],
) -> None:
    identity = json.dumps([kind, site, location, ref, _iso(at)], separators=(",", ":"))
    item_id = hashlib.sha256(identity.encode()).hexdigest()
    connection.execute(
        "INSERT INTO dispatch.outbox (id, kind, payload, created_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (item_id, kind, Jsonb({"id": item_id, "kind": kind, "ref": ref, **payload}), at),
    )
