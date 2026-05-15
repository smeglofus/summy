import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from integrator.eshop_client import (
    EshopError,
    RemoteProductConflictError,
    RemoteProductMissingError,
)
from integrator.models import ProductSyncState
from integrator.sync_service import SyncService, _advisory_lock_id, _sku_lock, stable_hash


def _stats(
    *,
    created=0,
    updated=0,
    skipped=0,
    invalid=0,
    failed=0,
    retryable_failed=0,
):
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "invalid": invalid,
        "failed": failed,
        "retryable_failed": retryable_failed,
    }


@pytest.fixture
def erp_file(tmp_path: Path) -> Path:
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 100,
            "stocks": {"praha": 10},
            "attributes": {"color": "red"},
        },
        {
            "id": "SKU-B",
            "title": "B - negative price",
            "price_vat_excl": -50,
            "stocks": {"praha": 5},
            "attributes": {},
        },
    ]))
    return path


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.create_product.return_value = MagicMock(status_code=201)
    client.update_product.return_value = MagicMock(status_code=200)
    return client


@pytest.mark.django_db
def test_first_run_creates_new_products_and_skips_invalid(erp_file, mock_client):
    stats = SyncService(client=mock_client, data_path=erp_file).run()

    assert stats == _stats(created=1, invalid=1)
    assert ProductSyncState.objects.filter(sku="SKU-A", remote_exists=True).exists()
    assert not ProductSyncState.objects.filter(sku="SKU-B").exists()
    mock_client.create_product.assert_called_once()
    mock_client.update_product.assert_not_called()


@pytest.mark.django_db
def test_second_run_with_unchanged_data_skips(erp_file, mock_client):
    service = SyncService(client=mock_client, data_path=erp_file)
    service.run()
    mock_client.reset_mock()

    stats = service.run()

    assert stats == _stats(skipped=1, invalid=1)
    mock_client.create_product.assert_not_called()
    mock_client.update_product.assert_not_called()


@pytest.mark.django_db
def test_changed_payload_triggers_patch(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 100,
            "stocks": {"praha": 10},
            "attributes": {"color": "red"},
        }
    ]))
    SyncService(client=mock_client, data_path=path).run()
    mock_client.reset_mock()

    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 200,
            "stocks": {"praha": 10},
            "attributes": {"color": "red"},
        }
    ]))
    stats = SyncService(client=mock_client, data_path=path).run()

    assert stats == _stats(updated=1)
    mock_client.update_product.assert_called_once()
    args, _ = mock_client.update_product.call_args
    assert args[0] == "SKU-A"


@pytest.mark.django_db
def test_missing_remote_state_posts_even_if_local_state_exists(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 100,
            "stocks": {"praha": 10},
            "attributes": {"color": "red"},
        }
    ]))
    ProductSyncState.objects.create(sku="SKU-A", remote_exists=False)

    stats = SyncService(client=mock_client, data_path=path).run()

    assert stats == _stats(created=1)
    mock_client.create_product.assert_called_once()
    mock_client.update_product.assert_not_called()


@pytest.mark.django_db
def test_create_recovery_after_crash_patches_without_second_post(erp_file, mock_client):
    with patch.object(SyncService, "_mark_synced", side_effect=RuntimeError("crash")):
        with pytest.raises(RuntimeError, match="crash"):
            SyncService(client=mock_client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert state.create_in_progress is True
    assert state.remote_exists is False
    assert state.payload_hash is None
    assert state.pending_payload_hash is not None

    recovery_client = MagicMock()
    recovery_client.update_product.return_value = MagicMock(status_code=200)

    stats = SyncService(client=recovery_client, data_path=erp_file).run()

    state.refresh_from_db()
    assert stats == _stats(updated=1, invalid=1)
    recovery_client.update_product.assert_called_once()
    recovery_client.create_product.assert_not_called()
    assert state.create_in_progress is False
    assert state.remote_exists is True
    assert state.payload_hash is not None
    assert state.pending_payload_hash is None


@pytest.mark.django_db
def test_create_in_progress_patch_404_falls_back_to_post(erp_file):
    ProductSyncState.objects.create(sku="SKU-A", create_in_progress=True)
    client = MagicMock()
    client.update_product.side_effect = RemoteProductMissingError("missing")
    client.create_product.return_value = MagicMock(status_code=201)

    stats = SyncService(client=client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == _stats(created=1, invalid=1)
    assert [call[0] for call in client.method_calls] == ["update_product", "create_product"]
    assert state.remote_exists is True
    assert state.create_in_progress is False
    assert state.pending_payload_hash is None


@pytest.mark.django_db
def test_post_409_falls_back_to_patch(erp_file):
    client = MagicMock()
    client.create_product.side_effect = RemoteProductConflictError("exists")
    client.update_product.return_value = MagicMock(status_code=200)

    stats = SyncService(client=client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == _stats(updated=1, invalid=1)
    assert [call[0] for call in client.method_calls] == ["create_product", "update_product"]
    assert state.remote_exists is True
    assert state.create_in_progress is False
    assert state.pending_payload_hash is None


@pytest.mark.django_db
def test_payload_hash_is_saved_only_after_success(erp_file, mock_client):
    mock_client.create_product.side_effect = EshopError("remote failure")

    stats = SyncService(client=mock_client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == _stats(invalid=1, failed=1)
    assert state.payload_hash is None
    assert state.remote_exists is False
    assert state.create_in_progress is True
    assert state.pending_payload_hash is not None
    assert state.last_synced_at is None


@pytest.mark.django_db
def test_create_network_failure_keeps_create_in_progress_and_next_run_patches_first(
    erp_file,
):
    failing_client = MagicMock()
    failing_client.create_product.side_effect = EshopError("timeout", retryable=True)

    stats = SyncService(client=failing_client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == _stats(invalid=1, failed=1, retryable_failed=1)
    assert state.create_in_progress is True
    assert state.remote_exists is False
    assert state.payload_hash is None
    assert state.pending_payload_hash is not None

    recovery_client = MagicMock()
    recovery_client.update_product.return_value = MagicMock(status_code=200)

    stats = SyncService(client=recovery_client, data_path=erp_file).run()

    assert stats == _stats(updated=1, invalid=1)
    recovery_client.update_product.assert_called_once()
    recovery_client.create_product.assert_not_called()


@pytest.mark.django_db
def test_changed_payload_during_create_recovery_is_patched_first(tmp_path):
    old_payload = {
        "sku": "SKU-A",
        "title": "Old",
        "price_with_vat": "121.00",
        "total_stock": 1,
        "color": "red",
    }
    ProductSyncState.objects.create(
        sku="SKU-A",
        create_in_progress=True,
        pending_payload_hash=stable_hash(old_payload),
    )
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([{
        "id": "SKU-A",
        "title": "New",
        "price_vat_excl": 200,
        "stocks": {"praha": 5},
        "attributes": {"color": "blue"},
    }]))
    client = MagicMock()
    client.update_product.return_value = MagicMock(status_code=200)

    stats = SyncService(client=client, data_path=path).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == _stats(updated=1)
    _, sent_payload = client.update_product.call_args.args
    assert sent_payload["title"] == "New"
    assert sent_payload["price_with_vat"] == "242.00"
    assert state.pending_payload_hash is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("content", "expected_stats"),
    [
        ("{broken-json", _stats(failed=1)),
        (None, _stats(failed=1, retryable_failed=1)),
    ],
)
def test_loader_error_is_counted_as_failed(tmp_path, mock_client, caplog, content, expected_stats):
    path = tmp_path / "erp.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    logger = logging.getLogger("integrator.sync_service")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="integrator.sync_service"):
            stats = SyncService(client=mock_client, data_path=path).run()
    finally:
        logger.removeHandler(caplog.handler)

    assert stats == expected_stats
    assert "ERP data load failed" in caplog.text
    mock_client.create_product.assert_not_called()
    mock_client.update_product.assert_not_called()


def test_stable_hash_is_deterministic_and_order_independent():
    a = {"sku": "x", "title": "t", "color": "r", "total_stock": 1, "price_with_vat": "1.00"}
    b = {"price_with_vat": "1.00", "color": "r", "total_stock": 1, "title": "t", "sku": "x"}
    assert stable_hash(a) == stable_hash(b)


def test_stable_hash_differs_on_change():
    a = {"sku": "x", "price_with_vat": "1.00"}
    b = {"sku": "x", "price_with_vat": "2.00"}
    assert stable_hash(a) != stable_hash(b)


def test_advisory_lock_id_is_stable_signed_int64():
    lock_id = _advisory_lock_id("SKU-A")

    assert lock_id == _advisory_lock_id("SKU-A")
    assert -(2**63) <= lock_id < 2**63


@override_settings(ERP_SYNC_SKU_LOCK_TIMEOUT_SECONDS=0.01)
def test_in_process_sku_lock_timeout_is_retryable():
    with _sku_lock("SKU-LOCK"):
        with pytest.raises(EshopError) as exc_info:
            with _sku_lock("SKU-LOCK"):
                pass

    assert exc_info.value.retryable is True
