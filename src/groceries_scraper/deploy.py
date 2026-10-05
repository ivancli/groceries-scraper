"""Render a Kustomize Job template for one Site and Location."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from groceries_scraper.config import Site


def _dns_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


class JobTemplateError(ValueError):
    def __init__(self) -> None:
        super().__init__("stdin must contain exactly one batch/v1 Job with a container template")


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
            password = urlsplit(url).password
        except ValueError:
            raise ValueError("invalid --sink URL") from None
        if password is not None:
            raise ValueError(
                "--sink URLs must not contain a password; supply it through PGPASSWORD"
            )


def render_job(
    template: str,
    site: Site,
    site_config: Path,
    location: str,
    sinks: list[str],
    archive: str | None,
) -> str:
    job = _read_job(template)
    for label, value in (("Site", site.site), ("Location", location)):
        if len(value) > 63 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value
        ):
            raise ValueError(
                f"{label} name cannot be an exact Kubernetes label: use 1–63 letters, digits, "
                "`_`, `-` or `.`, starting and ending with a letter or digit"
            )
    metadata = _mapping(job, "metadata")
    metadata.pop("name", None)
    # Kubernetes adds five random characters; the completed Job name must fit in 63.
    name = f"scrape-{_dns_name(site.site)}-{_dns_name(location)}"
    metadata["generateName"] = name[:57].rstrip("-") + "-"
    labels = {"groceries-scraper/site": site.site, "groceries-scraper/location": location}
    _mapping(metadata, "labels").update(labels)
    pod = job["spec"]["template"]
    _mapping(_mapping(pod, "metadata"), "labels").update(labels)
    container = pod["spec"]["containers"][0]
    args = ["run", str(site_config), "--location", location]
    for sink in sinks:
        args.extend(["--sink", sink])
    if archive is not None:
        args.extend(["--archive", archive])
    template_args = container.get("args")
    if template_args is None:
        template_args = container["args"] = []
    if not isinstance(template_args, list) or any(
        not isinstance(arg, str) for arg in template_args
    ):
        raise JobTemplateError()
    template_args.extend(args)
    _refuse_sink_passwords(container["args"])
    secret_name = f"scrape-site-{_dns_name(site.site)}"
    container["envFrom"] = [
        entry
        for entry in _entries(container, "envFrom")
        if "secretRef" not in entry or _mapping(entry, "secretRef").get("name") != secret_name
    ] + [{"secretRef": {"name": secret_name, "optional": True}}]
    env: list[dict[str, Any]] = [
        entry for entry in _entries(container, "env") if entry.get("name") != "SCRAPE_JOB_NAME"
    ]
    container["env"] = env
    env.append(
        {
            "name": "SCRAPE_JOB_NAME",
            "valueFrom": {
                "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}
            },
        }
    )
    if any(page_type.render == "browser" for page_type in site.page_types.values()):
        pod["spec"]["volumes"] = [
            volume
            for volume in _entries(pod["spec"], "volumes")
            if volume.get("name") != "scrape-shm"
        ] + [{"name": "scrape-shm", "emptyDir": {"medium": "Memory"}}]
        container["volumeMounts"] = [
            mount
            for mount in _entries(container, "volumeMounts")
            if mount.get("mountPath") != "/dev/shm"
        ] + [{"name": "scrape-shm", "mountPath": "/dev/shm"}]
        resources = _mapping(container, "resources")
        _mapping(resources, "requests")["memory"] = "1Gi"
        _mapping(resources, "limits")["memory"] = "2Gi"
    return yaml.safe_dump(job, sort_keys=False)
