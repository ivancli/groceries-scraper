"""Render a Kustomize Job template for one Site and Location."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from groceries_scraper.config import Site
from groceries_scraper.config.models import RecordLevel
from groceries_scraper.run.supply import Supply

SITE_LABEL = "groceries-scraper/site"
LOCATION_LABEL = "groceries-scraper/location"
SHM_VOLUME = "scrape-shm"
SUPPLY_VOLUME = "supply"
SUPPLY_DIR = "/app/supply"
SUPPLY_KEY = "supply.jsonl"


class DeployError(Exception):
    pass


class JobTemplateError(DeployError):
    def __init__(self) -> None:
        super().__init__("stdin must contain exactly one batch/v1 Job with a container template")


def _dns_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


def supply_config_map(job: dict[str, Any], uid: str, supply: Supply) -> dict[str, Any]:
    """The rendered Job's supply, deleted with the Job."""
    metadata = job["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": metadata["name"],
            "labels": metadata.get("labels", {}),
            "ownerReferences": [
                {"apiVersion": "batch/v1", "kind": "Job", "name": metadata["name"], "uid": uid}
            ],
        },
        "data": {SUPPLY_KEY: supply.jsonl()},
    }


def job_prefix(site: str, location: str) -> str:
    # Kubernetes adds five random characters; the completed Job name must fit in 63.
    name = f"scrape-{_dns_name(site)}-{_dns_name(location)}"
    return name[:57].rstrip("-") + "-"


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = parent[key] = {}
    if not isinstance(value, dict):
        raise JobTemplateError()
    return value


def _entries(parent: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = parent.get(key)
    if value is None:
        value = parent[key] = []
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise JobTemplateError()
    return value


def _replace_entry(parent: dict[str, Any], key: str, field: str, new: dict[str, Any]) -> None:
    """Re-rendering an already rendered Job must not duplicate the entry."""
    parent[key] = [entry for entry in _entries(parent, key) if entry.get(field) != new[field]] + [
        new
    ]


def _read_job(template: str) -> dict[str, Any]:
    try:
        documents = list(yaml.safe_load_all(template))
        if len(documents) != 1:
            raise JobTemplateError()
        job = documents[0]
        if (
            not isinstance(job, dict)
            or job.get("kind") != "Job"
            or job.get("apiVersion") != "batch/v1"
        ):
            raise JobTemplateError()
        containers = job["spec"]["template"]["spec"]["containers"]
        if not isinstance(containers, list) or not containers:
            raise JobTemplateError()
        if any(
            not isinstance(c, dict) or not c.get("name") or not c.get("image") for c in containers
        ):
            raise JobTemplateError()
    except (yaml.YAMLError, KeyError, TypeError) as exc:
        raise JobTemplateError() from exc
    return job


def _refuse_sink_passwords(args: list[str]) -> None:
    for i, arg in enumerate(args):
        if arg == "--sink" and i + 1 < len(args):
            url = args[i + 1]
        elif arg.startswith("--sink="):
            url = arg.partition("=")[2]
        else:
            continue
        try:
            parts = urlsplit(url)
            password = parts.password
        except ValueError:
            raise DeployError("invalid --sink URL") from None
        # libpq also reads a password from the query string.
        if password is not None or "password" in parse_qs(parts.query, keep_blank_values=True):
            raise DeployError(
                "--sink URLs must not contain a password; supply it through PGPASSWORD"
            )


def _refuse_unlabelled_names(site: Site, location: str) -> None:
    for kind, value in (("Site", site.site), ("Location", location)):
        if len(value) > 63 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value
        ):
            raise DeployError(
                f"{kind} name cannot be an exact Kubernetes label: use 1–63 letters, digits, "
                "`_`, `-` or `.`, starting and ending with a letter or digit"
            )


def render_job(
    template: str,
    site: Site,
    site_config: Path,
    location: str,
    sink_urls: list[str],
    archive_url: str | None,
    *,
    supply_config_map: str | None = None,
    record: RecordLevel | None = None,
    run_id: str | None = None,
    name: str | None = None,
) -> str:
    """`name` replaces Kubernetes' generated name, for a Dispatcher that records it first."""
    if run_id is not None and supply_config_map is None:
        raise DeployError("--run-id needs --supply: only the Dispatcher assigns Run ids")
    job = _read_job(template)
    _refuse_unlabelled_names(site, location)
    metadata = _mapping(job, "metadata")
    metadata.pop("name", None)
    if name is None:
        metadata["generateName"] = job_prefix(site.site, location)
    else:
        metadata["name"] = name
    labels = {SITE_LABEL: site.site, LOCATION_LABEL: location}
    _mapping(metadata, "labels").update(labels)
    pod = job["spec"]["template"]
    _mapping(_mapping(pod, "metadata"), "labels").update(labels)
    container = pod["spec"]["containers"][0]

    args = container.get("args")
    if args is None:
        args = container["args"] = []
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise JobTemplateError()
    args.extend(["run", str(site_config), "--location", location])
    if supply_config_map is not None:
        args.extend(["--supply", f"{SUPPLY_DIR}/{SUPPLY_KEY}"])
        _replace_entry(
            pod["spec"],
            "volumes",
            "name",
            {"name": SUPPLY_VOLUME, "configMap": {"name": supply_config_map}},
        )
        _replace_entry(
            container,
            "volumeMounts",
            "mountPath",
            {"name": SUPPLY_VOLUME, "mountPath": SUPPLY_DIR, "readOnly": True},
        )
    if record is not None:
        args.extend(["--record", record])
    if run_id is not None:
        args.extend(["--run-id", run_id])
    for url in sink_urls:
        args.extend(["--sink", url])
    if archive_url is not None:
        args.extend(["--archive", archive_url])
    _refuse_sink_passwords(args)

    secret_ref = {"name": f"scrape-site-{_dns_name(site.site)}", "optional": True}
    container["envFrom"] = [
        entry
        for entry in _entries(container, "envFrom")
        if "secretRef" not in entry
        or _mapping(entry, "secretRef").get("name") != secret_ref["name"]
    ] + [{"secretRef": secret_ref}]
    _replace_entry(
        container,
        "env",
        "name",
        {
            "name": "SCRAPE_JOB_NAME",
            "valueFrom": {
                "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}
            },
        },
    )
    if any(page_type.render == "browser" for page_type in site.page_types.values()):
        # Chromium needs more shared memory than a container's default 64Mi.
        _replace_entry(
            pod["spec"], "volumes", "name", {"name": SHM_VOLUME, "emptyDir": {"medium": "Memory"}}
        )
        _replace_entry(
            container, "volumeMounts", "mountPath", {"name": SHM_VOLUME, "mountPath": "/dev/shm"}
        )
        resources = _mapping(container, "resources")
        _mapping(resources, "requests")["memory"] = "1Gi"
        _mapping(resources, "limits")["memory"] = "2Gi"
    return yaml.safe_dump(job, sort_keys=False)
