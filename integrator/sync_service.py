"""Orchestrates the full ERP -> e-shop sync flow.

``stable_hash`` is computed over ``NormalizedProduct.to_payload()`` only: sku,
title, price_with_vat, total_stock, and color. ERP fields outside this set do
not trigger a delta. Extend ``to_payload`` if a new field becomes externally
visible.
"""
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .eshop_client import (
    EshopClient,
    EshopError,
    RemoteProductConflictError,
    RemoteProductMissingError,
)
from .loader import ErpDataLoadError, load_erp_data
from .models import ProductSyncState
from .schemas import NormalizedProduct
from .transformer import transform_with_report

logger = logging.getLogger(__name__)
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def stable_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _advisory_lock_id(sku: str) -> int:
    raw = int.from_bytes(hashlib.sha256(sku.encode("utf-8")).digest()[:8], "big")
    if raw >= 2**63:
        raw -= 2**64
    return raw


@contextmanager
def _sku_lock(sku: str):
    timeout = max(float(settings.ERP_SYNC_SKU_LOCK_TIMEOUT_SECONDS), 0.0)
    poll_interval = max(float(settings.ERP_SYNC_SKU_LOCK_POLL_SECONDS), 0.01)

    if connection.vendor == "postgresql":
        lock_id = _advisory_lock_id(sku)
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while True:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                    acquired = bool(cursor.fetchone()[0])
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EshopError(
                        f"Timed out waiting for SKU lock sku={sku}",
                        retryable=True,
                    )
                time.sleep(min(poll_interval, remaining))
            yield
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
        return

    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.setdefault(sku, threading.Lock())
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise EshopError(
            f"Timed out waiting for in-process SKU lock sku={sku}",
            retryable=True,
        )
    try:
        yield
    finally:
        lock.release()


class SyncService:
    def __init__(
        self,
        client: EshopClient | None = None,
        data_path: Path | None = None,
    ):
        self.client = client or EshopClient()
        self.data_path = data_path or settings.ERP_DATA_PATH

    def run(self) -> dict:
        stats = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "invalid": 0,
            "failed": 0,
            "retryable_failed": 0,
        }

        try:
            raw_records = load_erp_data(self.data_path)
        except ErpDataLoadError as exc:
            logger.error("ERP data load failed: %s", exc)
            stats["failed"] += 1
            if exc.retryable:
                stats["retryable_failed"] += 1
            logger.info("Sync completed: %s", stats)
            return stats

        transform_result = transform_with_report(raw_records)
        products = transform_result.products
        stats["invalid"] = transform_result.invalid_count
        logger.info(
            "Loaded %d records: %d valid, %d invalid, %d duplicate valid SKUs",
            len(raw_records),
            len(products),
            transform_result.invalid_count,
            transform_result.duplicate_count,
        )

        for product in products:
            try:
                stats[self._sync_one(product)] += 1
            except EshopError as exc:
                logger.error("Sync failed for %s: %s", product.sku, exc)
                stats["failed"] += 1
                if exc.retryable:
                    stats["retryable_failed"] += 1

        logger.info("Sync completed: %s", stats)
        return stats

    def _sync_one(self, product: NormalizedProduct) -> str:
        payload = product.to_payload()
        new_hash = stable_hash(payload)

        with _sku_lock(product.sku):
            state, _ = ProductSyncState.objects.get_or_create(sku=product.sku)

            if state.remote_exists:
                if state.payload_hash == new_hash:
                    if state.create_in_progress:
                        self._clear_create_in_progress(state)
                    return "skipped"
                return self._patch_or_create(state, payload, new_hash)

            if state.create_in_progress:
                return self._recover_create_in_progress(state, payload, new_hash)

            self._mark_create_in_progress(state, new_hash)
            return self._create_or_patch(state, payload, new_hash)

    def _recover_create_in_progress(
        self,
        state: ProductSyncState,
        payload: dict,
        payload_hash: str,
    ) -> str:
        if state.pending_payload_hash and state.pending_payload_hash != payload_hash:
            logger.warning(
                "ERP payload changed while create recovery was pending for sku=%s",
                state.sku,
            )
        return self._patch_or_create(state, payload, payload_hash)

    def _patch_or_create(self, state: ProductSyncState, payload: dict, payload_hash: str) -> str:
        try:
            response = self.client.update_product(state.sku, payload)
        except RemoteProductMissingError:
            logger.warning("Remote product missing for sku=%s during PATCH; trying POST", state.sku)
            self._mark_create_in_progress(state, payload_hash)
            return self._create_or_patch(state, payload, payload_hash)

        self._mark_synced(state, payload_hash, response.status_code)
        return "updated"

    def _create_or_patch(self, state: ProductSyncState, payload: dict, payload_hash: str) -> str:
        try:
            response = self.client.create_product(payload)
        except RemoteProductConflictError:
            logger.warning("Remote product conflict for sku=%s during POST; trying PATCH", state.sku)
            response = self.client.update_product(state.sku, payload)
            self._mark_synced(state, payload_hash, response.status_code)
            return "updated"

        self._mark_synced(state, payload_hash, response.status_code)
        return "created"

    @staticmethod
    def _mark_create_in_progress(state: ProductSyncState, payload_hash: str) -> None:
        if (
            state.create_in_progress
            and not state.remote_exists
            and state.pending_payload_hash == payload_hash
        ):
            return
        state.create_in_progress = True
        state.remote_exists = False
        state.pending_payload_hash = payload_hash
        state.save(update_fields=["create_in_progress", "remote_exists", "pending_payload_hash"])

    @staticmethod
    def _clear_create_in_progress(state: ProductSyncState) -> None:
        state.create_in_progress = False
        state.pending_payload_hash = None
        state.save(update_fields=["create_in_progress", "pending_payload_hash"])

    @staticmethod
    def _mark_synced(state: ProductSyncState, payload_hash: str, status_code: int) -> None:
        state.payload_hash = payload_hash
        state.remote_exists = True
        state.create_in_progress = False
        state.pending_payload_hash = None
        state.last_remote_status = status_code
        state.last_synced_at = timezone.now()
        state.save(
            update_fields=[
                "payload_hash",
                "remote_exists",
                "create_in_progress",
                "pending_payload_hash",
                "last_remote_status",
                "last_synced_at",
            ]
        )
