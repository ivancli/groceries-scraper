import ast
from pathlib import Path

import groceries_scraper.engine

# ADR-0001: the engine must run over Captures without a live crawl.
FORBIDDEN = {"scrapy", "twisted"}


def _imported_modules(source: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_engine_never_imports_scrapy_or_twisted() -> None:
    engine_dir = Path(groceries_scraper.engine.__file__).parent
    violations = [
        f"{path.relative_to(engine_dir)}: {module}"
        for path in sorted(engine_dir.rglob("*.py"))
        for module in sorted(_imported_modules(path))
        if module.split(".")[0] in FORBIDDEN
    ]

    assert violations == []
