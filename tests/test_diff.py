import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from groceries_scraper.cli import app
from groceries_scraper.run.diff import Change, DiffError, RecordTypeDiff, diff_runs

KEYED = {"product": {"key": ["sku"]}}


def _run(
    root: Path,
    run_id: str,
    records: dict[str, list[dict[str, Any]]],
    *,
    site: str = "shop",
    record_types: dict[str, Any] = KEYED,
    finish_reason: str = "finished",
) -> Path:
    path = root / site / run_id
    (path / "records").mkdir(parents=True)
    manifest = {
        "site": site,
        "run_id": run_id,
        "config": {"site": site, "records": record_types},
        "stats": {"finish_reason": finish_reason},
    }
    (path / "run.json").write_text(json.dumps(manifest))
    for record_type, rows in records.items():
        lines = [json.dumps({**row, "_meta": {"run_id": run_id}}) for row in rows]
        (path / "records" / f"{record_type}.jsonl").write_text("".join(f"{x}\n" for x in lines))
    return path


def test_records_are_matched_by_record_key_into_added_removed_and_changed(
    tmp_path: Path,
) -> None:
    old = _run(
        tmp_path,
        "1",
        {
            "product": [
                {"sku": "a", "price": 1.0, "name": "A"},
                {"sku": "b", "price": 2.0, "name": "B"},
                {"sku": "c", "price": 3.0, "name": "C"},
            ]
        },
    )
    new = _run(
        tmp_path,
        "2",
        {
            "product": [
                {"sku": "d", "price": 4.0, "name": "D"},
                {"sku": "b", "price": 2.5, "name": "B2"},
                {"sku": "a", "price": 1.0, "name": "A"},
            ]
        },
    )

    diff = diff_runs(old, new)

    assert diff.record_types == [
        RecordTypeDiff(
            "product",
            ["sku"],
            added=[{"sku": "d", "price": 4.0, "name": "D"}],
            removed=[{"sku": "c", "price": 3.0, "name": "C"}],
            changed=[Change({"sku": "b"}, {"price": (2.0, 2.5), "name": ("B", "B2")})],
            unchanged=1,
        )
    ]
    assert diff.warnings == []


def test_a_field_filter_reports_only_changes_to_that_field(tmp_path: Path) -> None:
    old = _run(
        tmp_path,
        "1",
        {"product": [{"sku": "a", "price": 1.0}, {"sku": "b", "price": 2.0, "name": "B"}]},
    )
    new = _run(
        tmp_path,
        "2",
        {"product": [{"sku": "a", "price": 1.5}, {"sku": "b", "price": 2.0, "name": "B2"}]},
    )

    [product] = diff_runs(old, new, only_field="price").record_types

    assert product.changed == [Change({"sku": "a"}, {"price": (1.0, 1.5)})]
    assert product.unchanged == 1


def test_a_field_absent_from_one_record_changes_from_or_to_null(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {"product": [{"sku": "a", "promo": True}]})
    new = _run(tmp_path, "2", {"product": [{"sku": "a", "unit": "kg"}]})

    [product] = diff_runs(old, new).record_types

    assert product.changed == [Change({"sku": "a"}, {"promo": (True, None), "unit": (None, "kg")})]


def test_record_types_without_a_record_key_are_skipped(tmp_path: Path) -> None:
    record_types = {"product": {"key": ["sku"]}, "promotion": {}}
    old = _run(tmp_path, "1", {"promotion": [{"x": 1}]}, record_types=record_types)
    new = _run(tmp_path, "2", {"product": [{"sku": "a"}]}, record_types=record_types)

    diff = diff_runs(old, new)

    assert [d.record_type for d in diff.record_types] == ["product"]
    assert diff.record_types[0].added == [{"sku": "a"}]


def test_a_record_type_keyed_only_in_the_older_run_shows_its_records_as_removed(
    tmp_path: Path,
) -> None:
    old = _run(
        tmp_path,
        "1",
        {"promotion": [{"id": 1}]},
        record_types={**KEYED, "promotion": {"key": ["id"]}},
    )
    new = _run(tmp_path, "2", {})

    diff = diff_runs(old, new)

    assert [(d.record_type, d.removed) for d in diff.record_types] == [
        ("product", []),
        ("promotion", [{"id": 1}]),
    ]


def test_a_field_no_record_has_cannot_be_compared(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {"product": [{"sku": "a", "price": 1.0}]})
    new = _run(tmp_path, "2", {"product": [{"sku": "a", "price": 1.0}]})

    with pytest.raises(DiffError, match="no Record in either Run has Field `prise`"):
        diff_runs(old, new, only_field="prise")


def test_runs_of_different_sites_cannot_be_compared(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, site="one")
    new = _run(tmp_path, "2", {}, site="two")

    with pytest.raises(DiffError, match="different Sites: one and two"):
        diff_runs(old, new)


def test_a_record_key_changed_between_runs_cannot_be_compared(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, record_types={"product": {"key": ["sku"]}})
    new = _run(tmp_path, "2", {}, record_types={"product": {"key": ["url"]}})

    with pytest.raises(DiffError, match=r"Record Key of `product` differs: \[sku\] and \[url\]"):
        diff_runs(old, new)


def test_runs_without_any_record_key_cannot_be_compared(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, record_types={"product": {}})
    new = _run(tmp_path, "2", {}, record_types={"product": {}})

    with pytest.raises(DiffError, match="no Record Type declares a Record Key"):
        diff_runs(old, new)


def test_a_directory_without_run_json_is_not_a_run(tmp_path: Path) -> None:
    new = _run(tmp_path, "2", {})

    with pytest.raises(DiffError, match="is not a Run directory"):
        diff_runs(tmp_path, new)


def test_a_run_that_did_not_finish_is_warned_about(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, finish_reason="closespider_itemcount")
    new = _run(tmp_path, "2", {})

    assert diff_runs(old, new).warnings == [
        "Run 1 did not finish (closespider_itemcount): Records it missed show as added or removed"
    ]


def _price_runs(tmp_path: Path) -> tuple[Path, Path]:
    old = _run(
        tmp_path,
        "1",
        {"product": [{"sku": "a", "price": 1.0, "name": "A"}, {"sku": "c", "price": 3.0}]},
    )
    new = _run(
        tmp_path,
        "2",
        {"product": [{"sku": "a", "price": 1.5, "name": "A2"}, {"sku": "d", "price": 4.0}]},
    )
    return old, new


def test_cli_diff_prints_counts_and_each_record_by_key(tmp_path: Path) -> None:
    old, new = _price_runs(tmp_path)

    result = CliRunner().invoke(app, ["diff", str(old), str(new)])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "product (key: sku): 1 added, 1 removed, 1 changed, 0 unchanged",
        '  + {"sku": "d"}',
        '  - {"sku": "c"}',
        '  ~ {"sku": "a"} name: "A" -> "A2"; price: 1.0 -> 1.5',
    ]


def test_cli_diff_field_option_narrows_changes(tmp_path: Path) -> None:
    old, new = _price_runs(tmp_path)

    result = CliRunner().invoke(app, ["diff", str(old), str(new), "--field", "price"])

    assert '  ~ {"sku": "a"} price: 1.0 -> 1.5' in result.output.splitlines()


def test_cli_diff_json_has_full_records_and_old_new_values(tmp_path: Path) -> None:
    old, new = _price_runs(tmp_path)

    result = CliRunner().invoke(app, ["diff", str(old), str(new), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "site": "shop",
        "old": "1",
        "new": "2",
        "warnings": [],
        "record_types": [
            {
                "record_type": "product",
                "key": ["sku"],
                "added": [{"sku": "d", "price": 4.0}],
                "removed": [{"sku": "c", "price": 3.0}],
                "changed": [
                    {
                        "key": {"sku": "a"},
                        "fields": {
                            "name": {"old": "A", "new": "A2"},
                            "price": {"old": 1.0, "new": 1.5},
                        },
                    }
                ],
                "unchanged": 0,
            }
        ],
    }


def test_cli_diff_warns_on_stderr_about_an_unfinished_run(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, finish_reason="shutdown")
    new = _run(tmp_path, "2", {})

    result = CliRunner().invoke(app, ["diff", str(old), str(new), "--json"])

    assert result.exit_code == 0
    assert "warning: Run 1 did not finish (shutdown)" in result.stderr
    assert json.loads(result.stdout)["warnings"] != []


def test_cli_diff_exits_1_when_runs_cannot_be_compared(tmp_path: Path) -> None:
    old = _run(tmp_path, "1", {}, site="one")
    new = _run(tmp_path, "2", {}, site="two")

    result = CliRunner().invoke(app, ["diff", str(old), str(new)])

    assert result.exit_code == 1
    assert "Cannot diff Runs: Runs are of different Sites: one and two" in result.stderr
