"""ERP record validation and normalization."""
import logging
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from django.conf import settings

from .models import ProductSyncState
from .schemas import NormalizedProduct

DEFAULT_COLOR = "N/A"
SKU_MAX_LENGTH = ProductSyncState._meta.get_field("sku").max_length
_CENT = Decimal("0.01")

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when a single ERP record fails validation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TransformResult:
    products: list[NormalizedProduct]
    invalid_count: int
    duplicate_count: int


def transform(records: Iterable[dict]) -> list[NormalizedProduct]:
    """Validate and normalize ERP records, skipping invalid records."""
    return transform_with_report(records).products


def transform_with_report(records: Iterable[dict]) -> TransformResult:
    """Validate records and keep the last valid record for duplicate SKUs."""
    products_by_sku: "OrderedDict[str, NormalizedProduct]" = OrderedDict()
    invalid_count = 0
    duplicate_count = 0

    for record in records:
        if not isinstance(record, dict):
            _log_skip("?", "not_an_object")
            invalid_count += 1
            continue
        raw_sku = record.get("id")
        if not isinstance(raw_sku, str) or not raw_sku.strip():
            _log_skip("?", "missing_sku")
            invalid_count += 1
            continue
        sku = raw_sku.strip()
        if len(sku) > SKU_MAX_LENGTH:
            _log_skip(sku, "sku_too_long")
            invalid_count += 1
            continue

        try:
            product = _normalize(sku, record)
        except ValidationError as exc:
            _log_skip(sku, exc.reason)
            invalid_count += 1
            continue

        if sku in products_by_sku:
            duplicate_count += 1
            logger.warning("Duplicate ERP record sku=%s; keeping last record", sku)
        products_by_sku[sku] = product

    return TransformResult(
        products=list(products_by_sku.values()),
        invalid_count=invalid_count,
        duplicate_count=duplicate_count,
    )


def _log_skip(sku: str, reason: str) -> None:
    logger.warning("Skipping ERP record sku=%s reason=%s", sku, reason)


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
    if not price.is_finite():
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
            if qty < 0:
                raise ValidationError("negative_stock")
            total += qty
            has_integer = True
            continue
        if isinstance(qty, float):
            raise ValidationError("invalid_stock_type")
        if isinstance(qty, str) and qty.strip().upper() == "N/A":
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
