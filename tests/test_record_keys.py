from groceries_scraper.config.models import RecordType
from groceries_scraper.engine.extract import Record
from groceries_scraper.run.keys import KeyRejection, RecordKeys


def _keys() -> RecordKeys:
    return RecordKeys({"product": RecordType(key=["sku", "store"]), "promotion": RecordType()})


def test_the_first_record_with_a_key_is_admitted_and_later_ones_are_duplicates() -> None:
    keys = _keys()

    assert keys.admit(Record("product", {"sku": "p1", "store": 1, "price": 1.0}), 3) is None
    assert keys.admit(Record("product", {"sku": "p1", "store": 1, "price": 2.0}), 7) == (
        KeyRejection(
            'duplicate Record Key {"sku": "p1", "store": 1} (first in Capture 3)',
            "duplicate Record Key",
        )
    )


def test_records_differing_in_any_key_field_are_distinct() -> None:
    keys = _keys()

    assert keys.admit(Record("product", {"sku": "p1", "store": 1}), 1) is None
    assert keys.admit(Record("product", {"sku": "p1", "store": 2}), 1) is None
    assert keys.admit(Record("product", {"sku": "p2", "store": 1}), 1) is None


def test_a_null_or_absent_key_field_rejects_the_record() -> None:
    keys = _keys()

    assert keys.admit(Record("product", {"sku": None, "store": 1}), 1) == KeyRejection(
        "Record Key Field `sku` is missing", "Record Key Field `sku` is missing"
    )
    assert keys.admit(Record("product", {"sku": "p1"}), 1) == KeyRejection(
        "Record Key Field `store` is missing", "Record Key Field `store` is missing"
    )


def test_record_types_without_a_key_are_never_deduplicated() -> None:
    keys = _keys()

    assert keys.admit(Record("promotion", {"price": 1.0}), 1) is None
    assert keys.admit(Record("promotion", {"price": 1.0}), 1) is None


def test_keys_are_per_record_type_and_compare_structured_values() -> None:
    keys = RecordKeys({"a": RecordType(key=["id"]), "b": RecordType(key=["id"])})

    assert keys.admit(Record("a", {"id": {"x": 1, "y": [2]}}), 1) is None
    assert keys.admit(Record("b", {"id": {"x": 1, "y": [2]}}), 1) is None
    assert keys.admit(Record("a", {"id": {"y": [2], "x": 1}}), 2) is not None
