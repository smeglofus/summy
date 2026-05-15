"""Orchestrates the full ERP -> e-shop sync flow.

Note on hash coverage: ``stable_hash`` is computed over ``NormalizedProduct.to_payload()``
only — sku, title, price_with_vat, total_stock, color. ERP fields outside this set
(e.g. ``attributes.material``) do NOT trigger a delta. Extend ``to_payload`` if a new
field becomes externally visible.
"""
import hashlib
import json
import logging
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .eshop_client import EshopClient, EshopError, RemoteProductMissingError
from .loader import load_erp_data
from .models import ProductSyncState, QuarantinedProduct
from .schemas import NormalizedProduct
from .sync_lock import SyncRunLock
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
        run_lock: SyncRunLock | None = None,
    ):
        self.client = client or EshopClient()
        self.data_path = data_path or settings.ERP_DATA_PATH
        self.run_lock = run_lock or SyncRunLock()

    def run(self) -> dict:
        if not self.run_lock.acquire():
            logger.warning("Sync already in progress, skipping this tick")
            return {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "quarantined": 0,
                "resolved": 0,
                "locked": True,
            }
        try:
            return self._run_locked()
        finally:
            self.run_lock.release()

    def _run_locked(self) -> dict:
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
        with transaction.atomic():
            existing = QuarantinedProduct.objects.select_for_update().filter(
                sku=sku,
                resolved_at__isnull=True,
            ).first()
            if existing:
                existing.raw_payload = raw_payload
                existing.reason = reason
                existing.save(update_fields=["raw_payload", "reason", "last_seen_at"])
                return

            try:
                QuarantinedProduct.objects.create(
                    sku=sku,
                    raw_payload=raw_payload,
                    reason=reason,
                )
            except IntegrityError:
                existing = QuarantinedProduct.objects.select_for_update().get(
                    sku=sku,
                    resolved_at__isnull=True,
                )
                existing.raw_payload = raw_payload
                existing.reason = reason
                existing.save(update_fields=["raw_payload", "reason", "last_seen_at"])

    def _sync_one(self, product: NormalizedProduct) -> str:
        payload = product.to_payload()
        new_hash = stable_hash(payload)
        state, _ = ProductSyncState.objects.get_or_create(sku=product.sku)

        if state.remote_exists and state.payload_hash == new_hash and not state.create_in_progress:
            return "skipped"

        if state.remote_exists:
            response = self.client.update_product(product.sku, payload)
            self._mark_synced(state, new_hash, response.status_code)
            return "updated"

        return self._create_or_recover(state, product.sku, payload, new_hash)

    def _create_or_recover(self, state: ProductSyncState, sku: str, payload: dict, payload_hash: str) -> str:
        if state.create_in_progress:
            try:
                response = self.client.update_product(sku, payload)
            except RemoteProductMissingError:
                logger.warning(
                    "Recovery PATCH found no remote product for %s, retrying as POST create",
                    sku,
                )
            else:
                self._mark_synced(state, payload_hash, response.status_code)
                return "updated"
        else:
            state.create_in_progress = True
            state.save(update_fields=["create_in_progress"])

        response = self.client.create_product(payload)
        self._mark_synced(state, payload_hash, response.status_code)
        return "created"

    @staticmethod
    def _mark_synced(state: ProductSyncState, payload_hash: str, status_code: int) -> None:
        state.payload_hash = payload_hash
        state.remote_exists = True
        state.create_in_progress = False
        state.last_remote_status = status_code
        state.last_synced_at = timezone.now()
        state.save(
            update_fields=[
                "payload_hash",
                "remote_exists",
                "create_in_progress",
                "last_remote_status",
                "last_synced_at",
            ]
        )
