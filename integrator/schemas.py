from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class NormalizedProduct:
    """Validated ERP product ready to be pushed to the e-shop."""

    sku: str
    title: str
    price_with_vat: Decimal
    total_stock: int
    color: str

    def to_payload(self) -> dict:
        return {
            "sku": self.sku,
            "title": self.title,
            "price_with_vat": str(self.price_with_vat),
            "total_stock": self.total_stock,
            "color": self.color,
        }


@dataclass(frozen=True)
class QuarantineRecord:
    """ERP record rejected by validation."""

    sku: str
    raw: dict
    reason: str


class ValidationError(Exception):
    """Raised when a single ERP record fails validation. `reason` is a short tag."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
