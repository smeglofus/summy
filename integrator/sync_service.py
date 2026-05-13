"""Orchestrates the full ERP -> e-shop sync flow."""
import hashlib
import json
import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .eshop_client import EshopClient, EshopError
from .loader import load_erp_data
from .models import ProductSyncState, QuarantinedProduct
from .schemas import NormalizedProduct
from .transformer import transform

logger = logging.getLogger(__name__)


def stable_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SyncService:
    def __init__(self, client: EshopClient | None = None, data_path: Path | None = None):
        self.client = client or EshopClient()
        self.data_path = data_path or settings.ERP_DATA_PATH

    def run(self) -> dict:
        raw_records = load_erp_data(self.data_path)
        valid, quarantined = transform(raw_records)
        logger.info("Loaded %d records: %d valid, %d quarantined",
                    len(raw_records), len(valid), len(quarantined))

        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "quarantined": 0, "resolved": 0}

        for record in quarantined:
            self._record_quarantine(record.sku, record.raw, record.reason)
            stats["quarantined"] += 1

        valid_skus = {p.sku for p in valid}
        if valid_skus:
            stats["resolved"] = QuarantinedProduct.objects.filter(
                sku__in=valid_skus, resolved_at__isnull=True,
            ).update(resolved_at=timezone.now())

        for product in valid:
            try:
                stats[self._sync_one(product)] += 1
            except EshopError as exc:
                logger.error("Sync failed for %s: %s", product.sku, exc)
                stats["failed"] += 1

        logger.info("Sync completed: %s", stats)
        return stats

    @staticmethod
    def _record_quarantine(sku: str, raw_payload: dict, reason: str) -> None:
        existing = QuarantinedProduct.objects.filter(
            sku=sku,
            resolved_at__isnull=True,
        ).first()
        if existing:
            existing.raw_payload = raw_payload
            existing.reason = reason
            existing.save(update_fields=["raw_payload", "reason", "last_seen_at"])
            return

        QuarantinedProduct.objects.create(
            sku=sku,
            raw_payload=raw_payload,
            reason=reason,
        )

    def _sync_one(self, product: NormalizedProduct) -> str:
        payload = product.to_payload()
        new_hash = stable_hash(payload)
        state = ProductSyncState.objects.filter(sku=product.sku).first()

        if state and state.payload_hash == new_hash:
            return "skipped"

        if state:
            response = self.client.update_product(product.sku, payload)
            with transaction.atomic():
                state.payload_hash = new_hash
                state.last_remote_status = response.status_code
                state.save(update_fields=["payload_hash", "last_remote_status", "last_synced_at"])
            return "updated"

        response = self.client.create_product(payload)
        ProductSyncState.objects.create(
            sku=product.sku,
            payload_hash=new_hash,
            last_remote_status=response.status_code,
        )
        return "created"
