import logging
from decimal import Decimal
from pathlib import Path

import pytest

from integrator.loader import load_erp_data
from integrator.transformer import DEFAULT_COLOR, transform


def _by_sku(products):
    return {p.sku: p for p in products}


def _transform_with_log_capture(records, caplog):
    logger = logging.getLogger("integrator.transformer")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="integrator.transformer"):
            return transform(records)
    finally:
        logger.removeHandler(caplog.handler)


def _assert_skip_logged(caplog, sku: str, reason: str):
    assert f"sku={sku}" in caplog.text
    assert f"reason={reason}" in caplog.text


def test_happy_path_applies_vat_and_sums_stocks():
    raw = [
        {
            "id": "SKU-001",
            "title": "Espresso machine",
            "price_vat_excl": 12400.5,
            "stocks": {"praha": 5, "brno": 3},
            "attributes": {"color": "silver"},
        }
    ]
    products = transform(raw)
    p = products[0]
    assert p.sku == "SKU-001"
    assert p.total_stock == 8
    assert p.color == "silver"
    assert p.price_with_vat == Decimal("15004.61")


def test_negative_price_is_logged_and_skipped(caplog):
    raw = [{
        "id": "SKU-002",
        "title": "Bad discount",
        "price_vat_excl": -150.0,
        "stocks": {"praha": 10},
        "attributes": {},
    }]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "SKU-002", "non_positive_price")


def test_null_attributes_defaults_color():
    raw = [{
        "id": "SKU-003",
        "title": "Grinder",
        "price_vat_excl": 1500,
        "stocks": {"externi": 50},
        "attributes": None,
    }]
    products = transform(raw)
    assert products[0].color == DEFAULT_COLOR
    assert products[0].price_with_vat == Decimal("1815.00")


def test_null_price_is_logged_and_skipped(caplog):
    raw = [{
        "id": "SKU-004",
        "title": "Mug",
        "price_vat_excl": None,
        "stocks": {"praha": 10},
        "attributes": {"color": "black"},
    }]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "SKU-004", "missing_price")


def test_duplicate_sku_last_wins():
    raw = [
        {"id": "SKU-006", "title": "Tablets v1", "price_vat_excl": 250,
         "stocks": {"praha": 100}, "attributes": {}},
        {"id": "SKU-006", "title": "Tablets v2", "price_vat_excl": 300,
         "stocks": {"praha": 50}, "attributes": {}},
    ]
    products = transform(raw)
    assert len(products) == 1
    assert products[0].title == "Tablets v2"
    assert products[0].total_stock == 50
    assert products[0].price_with_vat == Decimal("363.00")


def test_partial_stock_with_na_uses_known_warehouses():
    raw = [{
        "id": "SKU-008",
        "title": "Filters",
        "price_vat_excl": 300,
        "stocks": {"praha": "N/A", "brno": 7},
        "attributes": {"color": "white"},
    }]
    products = transform(raw)
    assert products[0].total_stock == 7
    assert products[0].color == "white"


def test_all_stocks_unknown_is_logged_and_skipped(caplog):
    raw = [{
        "id": "SKU-008",
        "title": "Filters",
        "price_vat_excl": 300,
        "stocks": {"praha": "N/A"},
        "attributes": {"color": "white"},
    }]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "SKU-008", "no_known_stock")


@pytest.mark.parametrize(
    "stocks",
    [
        {"praha": 1.5},
        {"praha": True},
        {"praha": 1, "brno": 2.5},
        {"praha": "5"},
        {"praha": None},
        {"praha": 3.0},
        {"praha": Decimal("3")},
    ],
)
def test_invalid_stock_values_are_logged_and_skipped(stocks, caplog):
    raw = [{
        "id": "SKU-009",
        "title": "Stock issue",
        "price_vat_excl": 300,
        "stocks": stocks,
        "attributes": {"color": "white"},
    }]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "SKU-009", "invalid_stock_type")


def test_missing_sku_is_logged_and_skipped(caplog):
    raw = [{"title": "Anonymous", "price_vat_excl": 100, "stocks": {"a": 1}}]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "?", "missing_sku")


@pytest.mark.parametrize(
    "raw_price, expected_reason",
    [
        ("not a number", "invalid_price_type"),
        (True, "invalid_price_type"),
        (0, "non_positive_price"),
    ],
)
def test_invalid_price_types_are_logged_and_skipped(raw_price, expected_reason, caplog):
    raw = [{
        "id": "SKU-X",
        "title": "X",
        "price_vat_excl": raw_price,
        "stocks": {"praha": 1},
        "attributes": {},
    }]
    products = _transform_with_log_capture(raw, caplog)
    assert products == []
    _assert_skip_logged(caplog, "SKU-X", expected_reason)


def test_full_erp_dataset_matches_expectations():
    root = Path(__file__).resolve().parents[2]
    raw = load_erp_data(root / "erp_data.json")

    products = transform(raw)
    valid_by = _by_sku(products)

    assert set(valid_by) == {"SKU-001", "SKU-003", "SKU-006"}
