import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from integrator.eshop_client import EshopError
from integrator.models import ProductSyncState
from integrator.sync_service import SyncService, stable_hash


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

    assert stats == {"created": 1, "updated": 0, "skipped": 0, "failed": 0}
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

    assert stats == {"created": 0, "updated": 0, "skipped": 1, "failed": 0}
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

    assert stats == {"created": 0, "updated": 1, "skipped": 0, "failed": 0}
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

    assert stats == {"created": 1, "updated": 0, "skipped": 0, "failed": 0}
    mock_client.create_product.assert_called_once()
    mock_client.update_product.assert_not_called()


@pytest.mark.django_db
def test_payload_hash_is_saved_only_after_success(erp_file, mock_client):
    mock_client.create_product.side_effect = EshopError("remote failure")

    stats = SyncService(client=mock_client, data_path=erp_file).run()

    state = ProductSyncState.objects.get(sku="SKU-A")
    assert stats == {"created": 0, "updated": 0, "skipped": 0, "failed": 1}
    assert state.payload_hash is None
    assert state.remote_exists is False
    assert state.last_synced_at is None


def test_stable_hash_is_deterministic_and_order_independent():
    a = {"sku": "x", "title": "t", "color": "r", "total_stock": 1, "price_with_vat": "1.00"}
    b = {"price_with_vat": "1.00", "color": "r", "total_stock": 1, "title": "t", "sku": "x"}
    assert stable_hash(a) == stable_hash(b)


def test_stable_hash_differs_on_change():
    a = {"sku": "x", "price_with_vat": "1.00"}
    b = {"sku": "x", "price_with_vat": "2.00"}
    assert stable_hash(a) != stable_hash(b)
