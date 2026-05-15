import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.db import IntegrityError

from integrator.eshop_client import RemoteProductMissingError
from integrator.models import ProductSyncState, QuarantinedProduct
from integrator.sync_service import SyncService, stable_hash


class StubRunLock:
    def __init__(self, acquire_result: bool = True):
        self.acquire_result = acquire_result
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.acquire_result

    def release(self) -> None:
        self.release_calls += 1


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
def single_valid_erp_file(tmp_path: Path) -> Path:
    path = tmp_path / "erp-valid.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 100,
            "stocks": {"praha": 10},
            "attributes": {"color": "red"},
        }
    ]))
    return path


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.create_product.return_value = MagicMock(status_code=201)
    client.update_product.return_value = MagicMock(status_code=200)
    return client


@pytest.mark.django_db
def test_first_run_creates_new_products_and_quarantines_invalid(erp_file, mock_client):
    stats = SyncService(client=mock_client, data_path=erp_file).run()

    assert stats["created"] == 1
    assert stats["quarantined"] == 1
    assert stats["updated"] == 0
    assert stats["skipped"] == 0
    assert ProductSyncState.objects.filter(sku="SKU-A", remote_exists=True).exists()
    assert QuarantinedProduct.objects.filter(sku="SKU-B", reason="non_positive_price").exists()
    mock_client.create_product.assert_called_once()
    mock_client.update_product.assert_not_called()


@pytest.mark.django_db
def test_second_run_with_unchanged_data_skips(erp_file, mock_client):
    service = SyncService(client=mock_client, data_path=erp_file)
    service.run()
    mock_client.reset_mock()

    stats = service.run()
    assert stats["skipped"] == 1
    assert stats["updated"] == 0
    assert stats["created"] == 0
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

    assert stats["updated"] == 1
    assert stats["created"] == 0
    mock_client.update_product.assert_called_once()
    args, _ = mock_client.update_product.call_args
    assert args[0] == "SKU-A"


@pytest.mark.django_db
def test_previously_quarantined_sku_is_resolved_when_data_becomes_valid(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": -10,
            "stocks": {"praha": 1},
            "attributes": {},
        }
    ]))
    SyncService(client=mock_client, data_path=path).run()
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).exists()

    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": 100,
            "stocks": {"praha": 1},
            "attributes": {},
        }
    ]))
    stats = SyncService(client=mock_client, data_path=path).run()

    assert stats["created"] == 1
    assert stats["resolved"] == 1
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).count() == 0


@pytest.mark.django_db
def test_repeated_invalid_sku_refreshes_existing_open_quarantine(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "A",
            "price_vat_excl": -10,
            "stocks": {"praha": 1},
            "attributes": {},
        }
    ]))
    SyncService(client=mock_client, data_path=path).run()
    open_record = QuarantinedProduct.objects.get(sku="SKU-A", resolved_at__isnull=True)

    path.write_text(json.dumps([
        {
            "id": "SKU-A",
            "title": "",
            "price_vat_excl": 100,
            "stocks": {"praha": 1},
            "attributes": {},
        }
    ]))
    stats = SyncService(client=mock_client, data_path=path).run()

    refreshed = QuarantinedProduct.objects.get(sku="SKU-A", resolved_at__isnull=True)
    assert stats["quarantined"] == 1
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).count() == 1
    assert refreshed.pk == open_record.pk
    assert refreshed.reason == "missing_title"
    assert refreshed.raw_payload["title"] == ""


@pytest.mark.django_db
def test_open_quarantine_constraint_blocks_duplicates():
    QuarantinedProduct.objects.create(
        sku="SKU-A",
        raw_payload={"id": "SKU-A"},
        reason="missing_title",
    )

    with pytest.raises(IntegrityError):
        QuarantinedProduct.objects.create(
            sku="SKU-A",
            raw_payload={"id": "SKU-A"},
            reason="missing_price",
        )


def test_stable_hash_is_deterministic_and_order_independent():
    a = {"sku": "x", "title": "t", "color": "r", "total_stock": 1, "price_with_vat": "1.00"}
    b = {"price_with_vat": "1.00", "color": "r", "total_stock": 1, "title": "t", "sku": "x"}
    assert stable_hash(a) == stable_hash(b)


def test_stable_hash_differs_on_change():
    a = {"sku": "x", "price_with_vat": "1.00"}
    b = {"sku": "x", "price_with_vat": "2.00"}
    assert stable_hash(a) != stable_hash(b)


@pytest.mark.django_db
def test_run_skips_when_lock_not_acquired(erp_file, mock_client):
    run_lock = StubRunLock(acquire_result=False)

    stats = SyncService(client=mock_client, data_path=erp_file, run_lock=run_lock).run()

    assert stats.get("locked") is True
    assert run_lock.acquire_calls == 1
    assert run_lock.release_calls == 0
    mock_client.create_product.assert_not_called()


@pytest.mark.django_db
def test_run_releases_lock_after_success(erp_file, mock_client):
    run_lock = StubRunLock()

    SyncService(client=mock_client, data_path=erp_file, run_lock=run_lock).run()

    assert run_lock.acquire_calls == 1
    assert run_lock.release_calls == 1


@pytest.mark.django_db
def test_recovery_after_successful_create_before_state_finalization_uses_patch_not_post(
    single_valid_erp_file,
    mock_client,
    monkeypatch,
):
    service = SyncService(client=mock_client, data_path=single_valid_erp_file)
    original_mark_synced = SyncService._mark_synced
    payload_hashes: list[str] = []

    def crash_once(state, payload_hash, status_code):
        payload_hashes.append(payload_hash)
        raise RuntimeError("state write crashed after remote success")

    monkeypatch.setattr(SyncService, "_mark_synced", staticmethod(crash_once))

    with pytest.raises(RuntimeError, match="state write crashed after remote success"):
        service.run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert state.remote_exists is False
    assert state.create_in_progress is True
    assert state.payload_hash is None
    assert state.last_synced_at is None
    assert mock_client.create_product.call_count == 1
    assert mock_client.update_product.call_count == 0

    monkeypatch.setattr(SyncService, "_mark_synced", staticmethod(original_mark_synced))

    stats = service.run()

    state.refresh_from_db()
    assert stats["updated"] == 1
    assert stats["created"] == 0
    assert mock_client.create_product.call_count == 1
    assert mock_client.update_product.call_count == 1
    assert state.remote_exists is True
    assert state.create_in_progress is False
    assert state.payload_hash == payload_hashes[0]
    assert state.last_remote_status == 200
    assert state.last_synced_at is not None


@pytest.mark.django_db
def test_recovery_falls_back_to_post_when_remote_product_is_missing(single_valid_erp_file, mock_client):
    ProductSyncState.objects.create(
        sku="SKU-A",
        create_in_progress=True,
        remote_exists=False,
    )
    mock_client.update_product.side_effect = RemoteProductMissingError("remote SKU missing")

    stats = SyncService(client=mock_client, data_path=single_valid_erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats["created"] == 1
    assert mock_client.update_product.call_count == 1
    assert mock_client.create_product.call_count == 1
    assert state.remote_exists is True
    assert state.create_in_progress is False
    assert state.payload_hash == stable_hash(mock_client.create_product.call_args.args[0])
