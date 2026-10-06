"""`scrape dispatch`: one idempotent tick from the Watch List to Kubernetes Jobs."""

import json
import logging
import secrets
import string
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from groceries_scraper.config import Site, load_checked_site
from groceries_scraper.config.loader import DEFAULTS_PATH, load_min_every
from groceries_scraper.config.models import DEFAULT_LOCATION
from groceries_scraper.deploy import SUPPLY_KEY, job_prefix, render_job
from groceries_scraper.dispatch.collector import CollectorClient
from groceries_scraper.dispatch.store import DispatchStore, RefState
from groceries_scraper.run.directory import new_run_id

logger = logging.getLogger(__name__)

JOB_REFS = 200
MAX_BACKOFF = timedelta(hours=24)
JobStatus = Literal["running", "finished", "missing"]


class Cluster(Protocol):
    def create_job(self, job: dict[str, Any]) -> str:
        """Returns the created Job's uid."""
        ...

    def create_config_map(self, config_map: dict[str, Any]) -> None: ...

    def job_status(self, name: str) -> JobStatus: ...


@dataclass(frozen=True)
class _Route:
    site: Site
    path: Path
    location: str


@dataclass(frozen=True)
class _Unroutable:
    outcome: str  # a Collector API outcome
    site: str | None


@dataclass
class Dispatcher:
    store: DispatchStore
    collector: CollectorClient
    cluster: Cluster
    sites: Path
    job_template: str
    sink_url: str
    defaults: Path = DEFAULTS_PATH

    def tick(self, now: datetime, *, dry_run: bool = False) -> None:
        """A dry run validates the outbox with the tracker and logs the Jobs it would create."""
        if not dry_run:
            self._settle(now)
        self.collector.deliver_changes(dry_run=dry_run, now=now)
        if not dry_run:
            self.collector.refresh_watch_list()
            self.collector.publish_site_catalogue(self.sites)
        for route, requests in self._due(now, dry_run=dry_run):
            for start in range(0, len(requests), JOB_REFS):
                chunk = requests[start : start + JOB_REFS]
                if dry_run:
                    logger.info(
                        "Would dispatch %d refs to %s at %s",
                        len(chunk),
                        route.site.site,
                        route.location,
                    )
                else:
                    self._create_job(route, chunk, now)

    def _settle(self, now: datetime) -> None:
        for dispatched in self.store.in_flight():
            # Read before ingesting: a Job finishing in between must not look like a lost Run.
            status = self.cluster.job_status(dispatched.job_name)
            try:
                if self.store.ingest(dispatched.site, dispatched.run_id):
                    continue
            except ValueError as exc:
                logger.error("Run %s cannot be ingested: %s", dispatched.run_id, exc)
                self.store.abandon(dispatched.site, dispatched.run_id, str(exc), now)
                continue
            if status != "running":
                logger.warning("Job %s ended without a Run to ingest", dispatched.job_name)
                error = f"Job {status} without an exported Run"
                self.store.abandon(dispatched.site, dispatched.run_id, error, now)

    def _due(self, now: datetime, *, dry_run: bool) -> list[tuple[_Route, list[dict[str, str]]]]:
        sites = self._load_sites()
        min_every = load_min_every(self.defaults)
        states = self.store.ref_states()
        in_flight = {ref for dispatched in self.store.in_flight() for ref in dispatched.refs}
        batches: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        routes: dict[tuple[str, str], _Route] = {}
        urls: dict[tuple[str, str], set[str]] = defaultdict(set)
        _, entries = self.store.watch_list()
        for entry in entries:
            ref, url = entry["ref"], entry["url"]
            route = _route(sites, url, entry["location"])
            if isinstance(route, _Unroutable):
                if not dry_run:
                    location = entry["location"]
                    self.store.report_outcome(route.site, location, ref, route.outcome, now)
                continue
            assert route.site.schedule is not None
            if not route.site.schedule.enabled or ref in in_flight:
                continue
            state = states.get(ref)
            if not _is_due(state, route.site.schedule.every, min_every, entry["check_soon"], now):
                continue
            key = (route.site.site, route.location)
            # One fetch per URL per Run; a repeated URL waits for the next tick.
            if url in urls[key]:
                continue
            urls[key].add(url)
            routes[key] = route
            batches[key].append({"ref": ref, "url": url})
        return [(routes[key], batches[key]) for key in sorted(batches)]

    def _load_sites(self) -> list[tuple[Site, Path]]:
        """Sites with Accepts Rules, in name order; an invalid Site fails the tick."""
        paths = [*self.sites.glob("*.yaml"), *self.sites.glob("*.yml")]
        loaded = [(load_checked_site(path, self.defaults)[0], path) for path in sorted(paths)]
        accepting = [(site, path) for site, path in loaded if site.accepts and site.schedule]
        return sorted(accepting, key=lambda pair: pair[0].site)

    def _create_job(self, route: _Route, requests: list[dict[str, str]], now: datetime) -> None:
        run_id = new_run_id(now)
        # Named here, not by Kubernetes, so the dispatch is recorded before the Job exists.
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
        name = job_prefix(route.site.site, route.location) + suffix
        job = yaml.safe_load(
            render_job(
                self.job_template,
                route.site,
                route.path,
                route.location,
                [self.sink_url],
                None,
                supply_config_map=name,
                record="errors",
                run_id=run_id,
            )
        )
        metadata = job["metadata"]
        del metadata["generateName"]
        metadata["name"] = name
        refs = [request["ref"] for request in requests]
        self.store.record_dispatch(
            site=route.site.site,
            location=route.location,
            run_id=run_id,
            job_name=name,
            refs=refs,
            created_at=now,
        )
        try:
            uid = self.cluster.create_job(job)
        except Exception:
            self.store.cancel_dispatch(route.site.site, run_id)
            raise
        supply = "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
        config_map_metadata = {
            "name": name,
            "labels": metadata.get("labels", {}),
            "ownerReferences": [
                {"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": uid}
            ],
        }
        if "namespace" in metadata:
            config_map_metadata["namespace"] = metadata["namespace"]
        # Created after the Job, for its uid; the pod waits for the volume until then.
        self.cluster.create_config_map(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": config_map_metadata,
                "data": {SUPPLY_KEY: supply},
            }
        )
        logger.info(
            "Dispatched %d refs to %s at %s as Job %s",
            len(refs),
            route.site.site,
            route.location,
            name,
        )


def _route(sites: list[tuple[Site, Path]], url: str, location: str | None) -> _Route | _Unroutable:
    for site, path in sites:
        assert site.accepts is not None
        if not site.accepts.matches(url):
            continue
        if location is None:
            if site.locations:
                return _Unroutable("no_home_store", site.site)
            location = DEFAULT_LOCATION
        try:
            return _Route(site, path, site.resolve_location(location))
        except ValueError:
            return _Unroutable("unknown_location", site.site)
    return _Unroutable("no_site", None)


def _is_due(
    state: RefState | None,
    every: timedelta,
    min_every: timedelta,
    check_soon: bool,
    now: datetime,
) -> bool:
    if state is None or state.last_attempt_at is None:
        return True
    since_attempt = now - state.last_attempt_at
    if check_soon and since_attempt >= min_every:
        return True
    if state.consecutive_failures:
        backoff = every * (1 << min(state.consecutive_failures - 1, 16))
        return since_attempt >= min(backoff, MAX_BACKOFF)
    return state.last_success_at is None or now - state.last_success_at >= every
