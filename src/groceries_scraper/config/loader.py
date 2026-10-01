from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from groceries_scraper.config.models import Site
from groceries_scraper.config.semantics import check_site

# src/groceries_scraper/config/loader.py -> repo root; the project runs from a checkout.
DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "defaults.yaml"


class ConfigError(Exception):
    """A Site config failed to load; `errors` are `"<yaml path>: <message>"` lines."""

    def __init__(self, source: str, errors: list[str], warnings: list[str] | None = None) -> None:
        self.source = source
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("\n  ".join([f"invalid Site config {source}:", *errors]))


def load_site(path: Path, defaults_path: Path = DEFAULTS_PATH) -> Site:
    """Load a Site file, with its `settings:` merged over the defaults file."""
    return parse_site(_read_yaml(path), _read_yaml(defaults_path), source=str(path))


def load_checked_site(path: Path, defaults_path: Path = DEFAULTS_PATH) -> tuple[Site, list[str]]:
    """`load_site` plus semantic checks: what every command runs first."""
    site = load_site(path, defaults_path)
    findings = check_site(site)
    if findings.errors:
        raise ConfigError(str(path), findings.errors, findings.warnings)
    return site, findings.warnings


def parse_site(data: Any, defaults: Any, source: str = "<site>") -> Site:
    if isinstance(data, Mapping):
        settings = data.get("settings", {})
        if isinstance(defaults, Mapping) and isinstance(settings, Mapping):
            data = {**data, "settings": {**defaults, **settings}}
    try:
        return Site.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            source, [f"{_yaml_path(e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from None


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(str(path), [str(exc)]) from None


def _yaml_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif not (part.startswith("<") and part.endswith(">")):
            path += f".{part}" if path else part
    return path or "(root)"
