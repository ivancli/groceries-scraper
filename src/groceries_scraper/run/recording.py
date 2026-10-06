"""Persist a Run's config, HTTP Captures and Extraction Traces."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from jinja2 import meta, nodes
from jinja2.sandbox import SandboxedEnvironment

from groceries_scraper.config import Site
from groceries_scraper.run.directory import Run
from groceries_scraper.run.keys import DUPLICATE_KEY
from groceries_scraper.run.redaction import REDACTED, MetadataRedactor
from groceries_scraper.run.stats import RunStats
from groceries_scraper.run.summary import RunOutcome
from groceries_scraper.run.supply import OUTCOMES_FILE, SUPPLY_FILE, Supply, SupplyOutcomes


@dataclass(frozen=True)
class Capture:
    capture_no: int
    page_type: str
    meta: dict[str, Any]
    body: bytes


class RunRecorder:
    def __init__(
        self,
        run: Run,
        site: Site,
        replay_of: Run | None = None,
        job: str | None = None,
        supply: Supply | None = None,
    ) -> None:
        self.run = run
        self.stats = RunStats.for_site(site)
        self.supplied = supply is not None
        self.outcomes = SupplyOutcomes(supply or Supply(()))
        self.level = site.settings.record_level
        self.ignore_params = frozenset(site.replay.ignore_params)
        self._sensitive_headers = frozenset(
            name.lower()
            for name in ["Cookie", "Set-Cookie", "Authorization", *site.settings.redact_headers]
        )
        # Omit model defaults such as empty Pipes, which aren't valid YAML inputs.
        snapshot = site.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        templates = [
            rule["request"]
            for page_type in snapshot["page_types"].values()
            for rule in page_type.get("follow", [])
            if rule.get("request") is not None
        ]
        if snapshot.get("session") is not None:
            templates.extend(step["request"] for step in snapshot["session"]["setup"])
        self._sensitive_variables = self._header_variables(templates)
        for template in templates:
            if "headers" in template:
                template["headers"] = self.redact_headers(template["headers"])
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self._manifest: dict[str, Any] = {
            "site": run.site,
            "run_id": run.run_id,
            "location": run.location,
            "config": snapshot,
            "config_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        }
        if job:
            self._manifest["job"] = job
        if supply is not None:
            # Kept beside the Captures, so a Replay can supply the same Start Requests.
            supply.write(run.path / SUPPLY_FILE)
            self._manifest.update(supplied=True, supplied_refs=len(supply))
        if replay_of is not None:
            self._manifest["replay_of"] = {"site": replay_of.site, "run_id": replay_of.run_id}
            self._manifest["missing"] = []
        self._write_manifest()

    def record_missing(self, metadata: dict[str, Any]) -> None:
        """A Replay request no Capture matched; written to `run.json` when the Run finishes."""
        self.stats.add_missing()
        self._manifest["missing"].append(self.redact_metadata(metadata))

    def finish(self, outcome: RunOutcome) -> None:
        if self.supplied:
            self.outcomes.write(self.run.path / OUTCOMES_FILE)
        self._manifest.update(outcome.to_json())
        self._write_manifest()

    def _write_manifest(self) -> None:
        (self.run.path / "run.json").write_text(_json(self._manifest), encoding="utf-8")

    def redact_headers(self, headers: dict[str, Any]) -> dict[str, Any]:
        return {
            name: REDACTED if name.lower() in self._sensitive_headers else value
            for name, value in headers.items()
        }

    def _header_variables(self, templates: list[dict[str, Any]]) -> set[str]:
        sensitive = set()
        environment = SandboxedEnvironment()
        for template in templates:
            for name, source in template.get("headers", {}).items():
                if name.lower() not in self._sensitive_headers:
                    continue
                tree = environment.parse(source)
                names = meta.find_undeclared_variables(tree)
                sensitive.update(names - {"env", "session", "value"})
                accesses = [
                    node
                    for node in tree.find_all((nodes.Getattr, nodes.Getitem))
                    if isinstance(node, nodes.Getattr | nodes.Getitem)
                    and isinstance(node.node, nodes.Name)
                    and node.node.name == "session"
                ]
                session_uses = sum(node.name == "session" for node in tree.find_all(nodes.Name))
                if session_uses > len(accesses):
                    sensitive.add("session")
                for node in accesses:
                    if isinstance(node, nodes.Getattr):
                        sensitive.add(f"session.{node.attr}")
                    elif isinstance(node.arg, nodes.Const):
                        sensitive.add(f"session.{node.arg.value}")
                    else:
                        sensitive.add("session")
        return sensitive

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return MetadataRedactor(
            metadata, self._sensitive_variables, self._sensitive_headers
        ).redact()

    def record(self, capture: Capture, trace: dict[str, Any]) -> None:
        if self.level == "off":
            return
        status = capture.meta["response"]["status"]
        if self.level == "errors" and 200 <= status < 300 and not _has_error(trace):
            return
        # Page Type names are config strings, not paths.
        stem = f"{capture.capture_no:04d}-{quote(capture.page_type, safe='')}"
        captures, traces = self.run.path / "captures", self.run.path / "traces"
        captures.mkdir(exist_ok=True)
        traces.mkdir(exist_ok=True)
        (captures / f"{stem}.meta.json").write_text(_json(capture.meta), encoding="utf-8")
        (captures / f"{stem}.body").write_bytes(capture.body)
        (traces / f"{stem}.trace.json").write_text(
            _json({"capture_no": capture.capture_no, "page_type": capture.page_type, **trace}),
            encoding="utf-8",
        )


def _has_error(trace: dict[str, Any]) -> bool:
    if trace.get("error"):
        return True
    # A repeated Record Key is expected on listings, not an extraction failure.
    if any(list(entry["reason_kinds"]) != [DUPLICATE_KEY] for entry in trace.get("dropped", [])):
        return True
    steps = list(trace.get("loop") or [])
    for entry in [*trace.get("fields", []), *trace.get("follow", [])]:
        if entry.get("error"):
            return True
        steps.extend(entry["steps"])
    return any(step.get("error") for step in steps)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
