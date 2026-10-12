import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SCHEMA_LOCK = 0xC0081
# Functions a restricted caller may execute; recovery stays with the operator.
CALLER_FUNCTIONS = (
    "admit_search(text, text, text, timestamptz)",
    "end_search(text, bigint)",
    "acquire_permit(text, text, integer, integer, integer, text, bigint, boolean, boolean)",
    "release_permit(bigint)",
)

AdmissionRefused = Literal["busy", "replayed", "expired"]


class CoordinatorUnavailable(Exception):
    """Coordination state could not be read or written: refuse the work."""


@dataclass(frozen=True)
class Admitted:
    execution_id: str
    fence: int
    deadline_at: datetime


@dataclass(frozen=True)
class Policy:
    """Seconds between a retailer's request starts, and its requests in flight."""

    min_interval: float
    max_concurrent: int


@dataclass(frozen=True)
class Permit:
    id: int
    retailer: str
    granted_at: datetime


@dataclass(frozen=True)
class Wait:
    """Until the retailer's next start, or None while its requests in flight are at the limit."""

    until: datetime | None


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class Coordinator:
    """A caller holding only the restricted coordination role."""

    def __init__(self, dsn: str, *, connect_timeout: int = 2) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._connection: psycopg.Connection[tuple[Any, ...]] | None = None
        self._lock = threading.Lock()

    def admit_search(
        self, execution_id: str, work: str, owner: str, deadline: datetime
    ) -> Admitted | AdmissionRefused:
        """Reserve the one fresh-search slot; a known id keeps its fence and deadline."""
        outcome, fence, deadline_at = self._call(
            "SELECT * FROM coordination.admit_search(%s, %s, %s, %s)",
            (execution_id, work, owner, deadline),
        )
        if outcome == "admitted":
            return Admitted(execution_id, fence, deadline_at)
        refused: AdmissionRefused = outcome
        return refused

    def end_search(self, admitted: Admitted) -> bool:
        """Only once the execution's process tree is verified gone; False for a stale fence."""
        (ended,) = self._call(
            "SELECT coordination.end_search(%s, %s)", (admitted.execution_id, admitted.fence)
        )
        return bool(ended)

    def acquire_permit(
        self,
        retailer: str,
        holder: str,
        policy: Policy,
        *,
        lease: timedelta | None = None,
        execution: Admitted | None = None,
        paced: bool = True,
        charge: bool = False,
    ) -> Permit | Wait | Literal["stale"]:
        """A price Job's permit lapses with its `lease`; a search's needs its current admission.

        An unpaced permit counts only toward requests in flight. `charge` accounts for a
        request that has already been sent, whatever the limits.
        """
        if (lease is None) == (execution is None):
            raise ValueError("a permit needs either a lease or a search execution")
        permit_id, granted_at, wait_until, refused = self._call(
            "SELECT * FROM coordination.acquire_permit(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                retailer,
                holder,
                _ms(policy.min_interval),
                policy.max_concurrent,
                None if lease is None else _ms(lease.total_seconds()),
                None if execution is None else execution.execution_id,
                None if execution is None else execution.fence,
                paced,
                charge,
            ),
        )
        if refused == "stale":
            return "stale"
        if permit_id is None:
            return Wait(wait_until)
        return Permit(permit_id, retailer, granted_at)

    def release_permit(self, permit: Permit) -> None:
        self._call("SELECT 1 FROM coordination.release_permit(%s)", (permit.id,))

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _call(self, query: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
        with self._lock:
            try:
                if self._connection is None or self._connection.closed:
                    self._connection = psycopg.connect(
                        self._dsn,
                        autocommit=True,
                        connect_timeout=self._connect_timeout,
                        options="-c statement_timeout=2000",
                    )
                row = self._connection.execute(query, params).fetchone()
            except psycopg.Error as exc:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                raise CoordinatorUnavailable(str(exc)) from exc
        assert row is not None
        return row


@dataclass(frozen=True)
class CoordinatorAdmin:
    """The database owner: creates the restricted role and recovers stuck executions."""

    dsn: str

    def prepare(self, role: str, password: str) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            connection.execute(Path(__file__).with_name("schema.sql").read_text())
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            verb = "ALTER" if exists.fetchone() else "CREATE"
            connection.execute(
                sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA coordination TO {}").format(sql.Identifier(role))
            )
            for function in CALLER_FUNCTIONS:
                connection.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION coordination.{} TO {}").format(
                        sql.SQL(function), sql.Identifier(role)
                    )
                )

    def set_policies(self, policies: dict[str, Policy]) -> None:
        with psycopg.connect(self.dsn) as connection:
            for retailer, policy in policies.items():
                connection.execute(
                    "INSERT INTO coordination.retailer_policies VALUES (%s, %s, %s)"
                    " ON CONFLICT (retailer) DO UPDATE SET"
                    " min_interval_ms = excluded.min_interval_ms,"
                    " max_concurrent = excluded.max_concurrent",
                    (retailer, _ms(policy.min_interval), policy.max_concurrent),
                )

    def active_searches(self) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            return connection.execute(
                "SELECT execution_id, work, owner, fence, admitted_at, deadline_at"
                " FROM coordination.search_executions WHERE ended_at IS NULL"
            ).fetchall()

    def recover_search(self, execution_id: str, fence: int, owner: str, note: str) -> bool:
        """Frees the slot only once the operator has confirmed `owner` is terminated."""
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT coordination.recover_search(%s, %s, %s, %s)",
                (execution_id, fence, owner, note),
            ).fetchone()
        return bool(row and row[0])
