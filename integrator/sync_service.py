"""Orchestrates the full ERP -> e-shop sync flow.

``stable_hash`` is computed over ``NormalizedProduct.to_payload()`` only: sku,
title, price_with_vat, total_stock, and color. ERP fields outside this set do
not trigger a delta. Extend ``to_payload`` if a new field becomes externally
visible.
"""
import hashlib
import json
import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .eshop_client import EshopClient, EshopError
from .loader import load_erp_data
from .models import ProductSyncState
from .schemas import NormalizedProduct
from .transformer import transform

logger = logging.getLogger(__name__)


def stable_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SyncService:
    def __init__(
        self,
        client: EshopClient | None = None,
        data_path: Path | None = None,
    ):
        self.client = client or EshopClient()
        self.data_path = data_path or settings.ERP_DATA_PATH

    def run(self) -> dict:
        raw_records = load_erp_data(self.data_path)
        products = transform(raw_records)
        logger.info("Loaded %d records: %d valid", len(raw_records), len(products))

        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

        for product in products:
            try:
                stats[self._sync_one(product)] += 1
            except EshopError as exc:
                logger.error("Sync failed for %s: %s", product.sku, exc)
                stats["failed"] += 1

        logger.info("Sync completed: %s", stats)
        return stats

    def _sync_one(self, product: NormalizedProduct) -> str:
        payload = product.to_payload()
        new_hash = stable_hash(payload)
        state, _ = ProductSyncState.objects.get_or_create(sku=product.sku)

        if state.remote_exists:
            if state.payload_hash == new_hash:
                return "skipped"

            response = self.client.update_product(product.sku, payload)
            self._mark_synced(state, new_hash, response.status_code)
            return "updated"

        response = self.client.create_product(payload)
        self._mark_synced(state, new_hash, response.status_code)
        return "created"

    @staticmethod
    def _mark_synced(state: ProductSyncState, payload_hash: str, status_code: int) -> None:
        state.payload_hash = payload_hash
        state.remote_exists = True
        state.last_remote_status = status_code
        state.last_synced_at = timezone.now()
        state.save(
            update_fields=[
                "payload_hash",
                "remote_exists",
                "last_remote_status",
                "last_synced_at",
            ]
        )
