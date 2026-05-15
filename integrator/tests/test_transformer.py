from decimal import Decimal

import pytest

from integrator.transformer import DEFAULT_COLOR, transform


def _by_sku(products):
    return {p.sku: p for p in products}


def _quarantine_reasons(records):
    return {r.sku: r.reason for r in records}


def test_happy_path_applies_vat_and_sums_stocks():
    raw = [
        {
            "id": "SKU-001",
            "title": "Kávovar Espresso",
            "price_vat_excl": 12400.5,
            "stocks": {"praha": 5, "brno": 3},
            "attributes": {"color": "stříbrná"},
        }
    ]
    valid, quarantined = transform(raw)
    assert quarantined == []
    p = valid[0]
    assert p.sku == "SKU-001"
    assert p.total_stock == 8
    assert p.color == "stříbrná"
    # 12400.5 * 1.21 = 15004.605 -> half-up -> 15004.61
    assert p.price_with_vat == Decimal("15004.61")


def test_negative_price_is_quarantined():
    raw = [{"id": "SKU-002", "title": "Sleva - chyba", "price_vat_excl": -150.0,
            "stocks": {"praha": 10}, "attributes": {}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert _quarantine_reasons(quarantined) == {"SKU-002": "non_positive_price"}


def test_null_attributes_defaults_color():
    raw = [{"id": "SKU-003", "title": "Mlýnek", "price_vat_excl": 1500,
            "stocks": {"externi": 50}, "attributes": None}]
    valid, quarantined = transform(raw)
    assert quarantined == []
    assert valid[0].color == DEFAULT_COLOR
    assert valid[0].price_with_vat == Decimal("1815.00")


def test_null_price_is_quarantined():
    raw = [{"id": "SKU-004", "title": "Hrnek", "price_vat_excl": None,
            "stocks": {"praha": 10}, "attributes": {"color": "černá"}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert _quarantine_reasons(quarantined) == {"SKU-004": "missing_price"}


def test_duplicate_sku_last_wins():
    raw = [
        {"id": "SKU-006", "title": "Tablety v1", "price_vat_excl": 250,
         "stocks": {"praha": 100}, "attributes": {}},
        {"id": "SKU-006", "title": "Tablety v2", "price_vat_excl": 300,
         "stocks": {"praha": 50}, "attributes": {}},
    ]
    valid, quarantined = transform(raw)
    assert quarantined == []
    assert len(valid) == 1
    assert valid[0].title == "Tablety v2"
    assert valid[0].total_stock == 50
    assert valid[0].price_with_vat == Decimal("363.00")


def test_partial_stock_with_na_uses_known_warehouses():
    raw = [{"id": "SKU-008", "title": "Filtry", "price_vat_excl": 300,
            "stocks": {"praha": "N/A", "brno": 7}, "attributes": {"color": "bílá"}}]
    valid, quarantined = transform(raw)
    assert quarantined == []
    assert valid[0].total_stock == 7
    assert valid[0].color == "bílá"


def test_all_stocks_unknown_is_quarantined():
    raw = [{"id": "SKU-008", "title": "Filtry", "price_vat_excl": 300,
            "stocks": {"praha": "N/A"}, "attributes": {"color": "bílá"}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert _quarantine_reasons(quarantined) == {"SKU-008": "no_known_stock"}


@pytest.mark.parametrize(
    "stocks",
    [
        {"praha": 1.5},
        {"praha": True},
        {"praha": 1, "brno": 2.5},
        {"praha": "5"},
        # Whole-valued floats (3.0) are also quarantined — JSON has no integer
        # type distinction, but the ERP contract requires integer quantities;
        # silently coercing 3.0 -> 3 would hide a producer-side type bug.
        {"praha": 3.0},
        # Decimal isn't bool/int/float/str/None — falls into the catch-all reject.
        {"praha": Decimal("3")},
    ],
)
def test_invalid_stock_values_are_quarantined(stocks):
    raw = [{"id": "SKU-009", "title": "Stock issue", "price_vat_excl": 300,
            "stocks": stocks, "attributes": {"color": "white"}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert _quarantine_reasons(quarantined) == {"SKU-009": "invalid_stock_type"}


def test_missing_sku_is_quarantined():
    raw = [{"title": "Anonymous", "price_vat_excl": 100, "stocks": {"a": 1}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert quarantined[0].reason == "missing_sku"


@pytest.mark.parametrize(
    "raw_price, expected_reason",
    [
        ("not a number", "invalid_price_type"),
        (True, "invalid_price_type"),
        (0, "non_positive_price"),
    ],
)
def test_invalid_price_types_are_quarantined(raw_price, expected_reason):
    raw = [{"id": "SKU-X", "title": "X", "price_vat_excl": raw_price,
            "stocks": {"praha": 1}, "attributes": {}}]
    valid, quarantined = transform(raw)
    assert valid == []
    assert quarantined[0].reason == expected_reason


def test_full_erp_dataset_matches_expectations():
    """Sanity check across the full sample dataset shipped with the task."""
    raw = [
        {"id": "SKU-001", "title": "Kávovar Espresso", "price_vat_excl": 12400.5,
         "stocks": {"praha": 5, "brno": 3}, "attributes": {"color": "stříbrná"}},
        {"id": "SKU-002", "title": "Sleva - chyba", "price_vat_excl": -150.0,
         "stocks": {"praha": 10}, "attributes": {}},
        {"id": "SKU-003", "title": "Mlýnek", "price_vat_excl": 1500,
         "stocks": {"externi": 50}, "attributes": None},
        {"id": "SKU-004", "title": "Hrnek", "price_vat_excl": None,
         "stocks": {"praha": 10}, "attributes": {"color": "černá"}},
        {"id": "SKU-006", "title": "Tablety", "price_vat_excl": 250,
         "stocks": {"praha": 100}, "attributes": {}},
        {"id": "SKU-006", "title": "Tablety", "price_vat_excl": 250,
         "stocks": {"praha": 100}, "attributes": {}},
        {"id": "SKU-008", "title": "Filtry", "price_vat_excl": 300,
         "stocks": {"praha": "N/A"}, "attributes": {"color": "bílá"}},
    ]
    valid, quarantined = transform(raw)
    valid_by = _by_sku(valid)
    assert set(valid_by) == {"SKU-001", "SKU-003", "SKU-006"}
    assert _quarantine_reasons(quarantined) == {
        "SKU-002": "non_positive_price",
        "SKU-004": "missing_price",
        "SKU-008": "no_known_stock",
    }
