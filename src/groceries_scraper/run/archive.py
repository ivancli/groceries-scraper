"""Copies a finished Run's whole directory to object storage, for debugging; see ADR-0003."""

from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlsplit

from groceries_scraper.run.directory import SavedRun


class ArchiveUrlError(Exception):
    pass


class ArchiveError(Exception):
    """The Archive was unreachable or rejected the Run."""


class Archive(Protocol):
    def prepare(self) -> None:
        """Fails fast, before a crawl spends time on a Run that can't be archived."""

    def archive(self, run: SavedRun) -> str: ...


def open_archive(url: str) -> Archive:
    """Errors never echo the URL, which may carry credentials."""
    parsed = urlsplit(url)
    if parsed.scheme != "s3":
        raise ArchiveUrlError(
            f"unsupported archive scheme `{parsed.scheme}`: use s3://<bucket>[/<prefix>]"
        )
    if not parsed.netloc:
        raise ArchiveUrlError("an s3:// archive needs a bucket: s3://<bucket>[/<prefix>]")
    try:
        from groceries_scraper.run.s3_archive import S3Archive
    except ModuleNotFoundError as exc:
        if exc.name != "boto3":
            raise
        raise ArchiveUrlError("the s3 archive needs boto3: install groceries-scraper[s3]") from None
    return S3Archive(parsed.netloc, parsed.path.strip("/"))


def overlapping_sinks(archive_url: str, sink_urls: Sequence[str]) -> list[str]:
    """S3 Sinks prune objects their Run lacks, so one sharing a prefix could delete Archives."""
    archive = _bucket_and_prefix(archive_url)
    overlapping = []
    for url in sink_urls:
        sink = _bucket_and_prefix(url)
        if archive is None or sink is None or archive[0] != sink[0]:
            continue
        if _contains(archive[1], sink[1]) or _contains(sink[1], archive[1]):
            overlapping.append(url)
    return overlapping


def _bucket_and_prefix(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "s3":
        return None
    return parsed.netloc, parsed.path.strip("/")


def _contains(outer: str, inner: str) -> bool:
    return not outer or inner == outer or inner.startswith(f"{outer}/")
