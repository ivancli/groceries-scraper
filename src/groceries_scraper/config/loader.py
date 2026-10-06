from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from groceries_scraper.config.models import MIN_EVERY, Site, parse_duration
from groceries_scraper.config.semantics import Findings, check_site

# src/groceries_scraper/config/loader.py -> repo root; the project runs from a checkout.
DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "defaults.yaml"


class ConfigError(Exception):
    """A Site config failed to load; `findings` holds its errors and any warnings."""

    def __init__(self, source: str, findings: Findings) -> None:
        self.source = source
        self.findings = findings
        super().__init__("\n  ".join([f"invalid Site config {source}:", *findings.errors]))

    @property
    def errors(self) -> list[str]:
        return self.findings.errors


def load_site(path: Path, defaults_path: Path = DEFAULTS_PATH) -> Site:
    """Load a Site file, with its `settings:` merged over the defaults file."""
    return parse_site(_read_yaml(path), _read_yaml(defaults_path), source=str(path))


def load_checked_site(path: Path, defaults_path: Path = DEFAULTS_PATH) -> tuple[Site, Findings]:
    """`load_site` plus semantic checks: what every command runs first."""
    return checked_site(_read_yaml(path), _read_yaml(defaults_path), str(path))


def checked_site(data: Any, defaults: Any, source: str = "<site>") -> tuple[Site, Findings]:
    site = parse_site(data, defaults, source)
    findings = check_site(site)
    if findings.errors:
        raise ConfigError(source, findings)
    return site, findings


def parse_site(data: Any, defaults: Any, source: str = "<site>") -> Site:
    """`defaults` is defaults.yaml: Settings, plus `schedule.min_every`."""
    context = {}
    if isinstance(defaults, Mapping):
        defaults = dict(defaults)
        if (min_every := (defaults.pop("schedule", None) or {}).get(MIN_EVERY)) is not None:
            try:
                context[MIN_EVERY] = parse_duration(min_every)
            except PydanticCustomError as exc:
                message = f"defaults schedule.{MIN_EVERY}: {exc.message()}"
                raise ConfigError(source, Findings([message])) from None
    if isinstance(data, Mapping):
        settings = data.get("settings", {})
        if isinstance(defaults, Mapping) and isinstance(settings, Mapping):
            data = {**data, "settings": {**defaults, **settings}}
    try:
        return Site.model_validate(data, context=context)
    except ValidationError as exc:
        errors = [f"{_yaml_path(e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(source, Findings(errors)) from None


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(str(path), Findings([str(exc)])) from None


def _yaml_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif not (part.startswith("<") and part.endswith(">")):
            path += f".{part}" if path else part
    return path or "(root)"
