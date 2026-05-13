"""ERP record validation and normalization.

Rules (see AGENTS.md for rationale):
* Duplicates by SKU -> last record wins.
* Missing/null/non-positive price -> quarantine (we do NOT silently fix money).
* Stocks: sum integer warehouse values, allow "N/A" as an unknown sentinel.
  Non-integer quantities -> quarantine. If no warehouse reports an integer
  quantity -> quarantine.
* Missing/non-dict attributes -> color defaults to "N/A".
"""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from django.conf import settings

from .schemas import NormalizedProduct, QuarantineRecord, ValidationError

DEFAULT_COLOR = "N/A"
_CENT = Decimal("0.01")


def transform(records: Iterable[dict]) -> tuple[list[NormalizedProduct], list[QuarantineRecord]]:
    """Validate and normalize ERP records. Returns (valid, quarantined)."""
    deduped: "OrderedDict[str, dict]" = OrderedDict()
    quarantined: list[QuarantineRecord] = []

    for record in records:
        if not isinstance(record, dict):
            quarantined.append(QuarantineRecord(sku="?", raw={"value": record}, reason="not_an_object"))
            continue
        sku = record.get("id")
        if not isinstance(sku, str) or not sku.strip():
            quarantined.append(QuarantineRecord(sku="?", raw=record, reason="missing_sku"))
            continue
        deduped[sku] = record  # last wins

    valid: list[NormalizedProduct] = []
    for sku, record in deduped.items():
        try:
            valid.append(_normalize(sku, record))
        except ValidationError as exc:
            quarantined.append(QuarantineRecord(sku=sku, raw=record, reason=exc.reason))
    return valid, quarantined


def _normalize(sku: str, record: dict) -> NormalizedProduct:
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("missing_title")

    price_with_vat = _normalize_price(record.get("price_vat_excl"))
    total_stock = _normalize_stocks(record.get("stocks"))
    color = _normalize_color(record.get("attributes"))

    return NormalizedProduct(
        sku=sku,
        title=title.strip(),
        price_with_vat=price_with_vat,
        total_stock=total_stock,
        color=color,
    )


def _normalize_price(raw_price) -> Decimal:
    if raw_price is None:
        raise ValidationError("missing_price")
    if isinstance(raw_price, bool):
        raise ValidationError("invalid_price_type")
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError):
        raise ValidationError("invalid_price_type")
    if price <= 0:
        raise ValidationError("non_positive_price")

    vat_multiplier = Decimal("1") + Decimal(str(settings.VAT_RATE))
    return (price * vat_multiplier).quantize(_CENT, rounding=ROUND_HALF_UP)


def _normalize_stocks(raw_stocks) -> int:
    if not isinstance(raw_stocks, dict) or not raw_stocks:
        raise ValidationError("no_known_stock")
    total = 0
    has_integer = False
    for qty in raw_stocks.values():
        if isinstance(qty, bool):
            raise ValidationError("invalid_stock_type")
        if isinstance(qty, int):
            total += qty
            has_integer = True
            continue
        if isinstance(qty, float):
            raise ValidationError("invalid_stock_type")
        if isinstance(qty, str) and qty.strip().upper() == "N/A":
            continue
        if qty is None:
            continue
        raise ValidationError("invalid_stock_type")
    if not has_integer:
        raise ValidationError("no_known_stock")
    return total


def _normalize_color(raw_attributes) -> str:
    if not isinstance(raw_attributes, dict):
        return DEFAULT_COLOR
    color = raw_attributes.get("color")
    if not isinstance(color, str) or not color.strip():
        return DEFAULT_COLOR
    return color.strip()
