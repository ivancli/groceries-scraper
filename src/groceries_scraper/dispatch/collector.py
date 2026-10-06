"""HTTP client for the tracker's Collector API."""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from email.message import Message
from http.client import HTTPException
from pathlib import Path
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict

from groceries_scraper.config.loader import load_checked_site
from groceries_scraper.dispatch.store import DispatchStore

logger = logging.getLogger(__name__)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        # Access may redirect to a login page; credentials belong only on the configured origin.
        return None


class _Rejection(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    reason: str


class _DeliveryResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    acked: list[str]
    rejected: list[_Rejection]


class _WatchEntry(BaseModel):
    model_config = ConfigDict(strict=True)
    ref: str
    url: str
    location: str | None
    check_soon: bool


class _WatchList(BaseModel):
    model_config = ConfigDict(strict=True)
    version: str
    entries: list[_WatchEntry]


class CollectorClient:
    def __init__(self, tracker_url: str, store: DispatchStore, *, timeout: float = 30) -> None:
        self.store = store
        self.base_url = tracker_url.rstrip("/") + "/api/collector/"
        self.timeout = timeout
        self._http = build_opener(_NoRedirects())
        self._headers = {
            "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
            "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def refresh_watch_list(self) -> bool:
        etag = self.store.collector_state("watch_etag")
        headers = {**self._headers, **({"If-None-Match": etag} if etag else {})}
        request = Request(self.base_url + "watch-list", headers=headers)
        try:
            with self._http.open(request, timeout=self.timeout) as response:
                result = _WatchList.model_validate(json.load(response))
                new_etag = response.headers.get("ETag") or f'"{result.version}"'
            if len({entry.ref for entry in result.entries}) != len(result.entries):
                raise ValueError("duplicate Watch List refs")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status != 304:
                logger.error("Collector watch-list HTTP %s", status)
            return False
        except (URLError, OSError, HTTPException):
            logger.warning("Collector watch-list network error")
            return False
        except ValueError:
            logger.error("Collector watch-list invalid response")
            return False
        self.store.replace_watch_list([entry.model_dump() for entry in result.entries], new_etag)
        return True

    def publish_site_catalogue(self, sites_path: Path) -> bool:
        sites = []
        paths = [*sites_path.glob("*.yaml"), *sites_path.glob("*.yml")]
        for path in sorted(paths):
            site, _ = load_checked_site(path)
            if site.accepts is None or site.schedule is None:
                continue
            locations = [
                {"name": name, "label": values.get("label") or name}
                for name, values in sorted(site.locations.items())
            ] or [{"name": "default", "label": "default"}]
            sites.append(
                {
                    "site": site.site,
                    "accepts": site.accepts.url,
                    "locations": locations,
                    "store_specific": bool(site.locations),
                    "every_seconds": int(site.schedule.every.total_seconds()),
                    "enabled": site.schedule.enabled,
                }
            )
        body = json.dumps(
            {"sites": sorted(sites, key=lambda site: site["site"])},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(body).hexdigest()
        if self.store.collector_state("catalogue_hash") == digest:
            return False
        request = Request(
            self.base_url + "site-catalogue", data=body, headers=self._headers, method="PUT"
        )
        try:
            with self._http.open(request, timeout=self.timeout):
                pass
        except HTTPError as exc:
            status = exc.code
            exc.close()
            logger.error("Collector site-catalogue HTTP %s", status)
            return False
        except (URLError, OSError, HTTPException):
            logger.warning("Collector site-catalogue network error")
            return False
        self.store.save_collector_state("catalogue_hash", digest)
        return True

    def deliver_changes(self, *, dry_run: bool = False, now: datetime | None = None) -> int:
        at = now or datetime.now(UTC)
        delivered = 0
        offset = 0
        while batch := self.store.pending_batch(offset=offset):
            if not dry_run and any(
                item.next_attempt_at is not None and item.next_attempt_at > at for item in batch
            ):
                break
            request = Request(
                self.base_url + "changes" + ("?dry_run=1" if dry_run else ""),
                data=json.dumps({"items": [item.payload for item in batch]}).encode(),
                headers=self._headers,
                method="POST",
            )
            try:
                with self._http.open(request, timeout=self.timeout) as response:
                    result = _DeliveryResponse.model_validate(json.load(response))
                final_ids = [*result.acked, *(item.id for item in result.rejected)]
                if (
                    len(final_ids) != len(set(final_ids))
                    or not set(final_ids) <= {item.id for item in batch}
                    or (not dry_run and not final_ids)
                ):
                    raise ValueError("invalid acknowledgement ids")
            except HTTPError as exc:
                error = f"Collector changes HTTP {exc.code}"
                exc.close()
                logger.log(
                    logging.CRITICAL if exc.code == 400 else logging.ERROR,
                    "%s; delivery stopped",
                    error,
                )
                if not dry_run:
                    self.store.fail_delivery([item.id for item in batch], error, at)
                break
            except (URLError, OSError, HTTPException):
                error = "Collector changes network error"
                logger.warning("%s; retrying on a later tick", error)
                if not dry_run:
                    self.store.fail_delivery([item.id for item in batch], error, at)
                break
            except ValueError:
                error = "Collector changes invalid response"
                logger.error("%s; delivery stopped", error)
                if not dry_run:
                    self.store.fail_delivery([item.id for item in batch], error, at)
                break
            if dry_run:
                offset += len(batch)
                continue
            acked = result.acked
            rejected = {item.id: item.reason for item in result.rejected}
            self.store.finish_delivery(acked, rejected, at)
            delivered += len(acked) + len(rejected)
        return delivered
