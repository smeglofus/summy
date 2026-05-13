import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from integrator.models import ProductSyncState, QuarantinedProduct
from integrator.sync_service import SyncService, stable_hash


@pytest.fixture
def erp_file(tmp_path: Path) -> Path:
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([
        {"id": "SKU-A", "title": "A", "price_vat_excl": 100,
         "stocks": {"praha": 10}, "attributes": {"color": "red"}},
        {"id": "SKU-B", "title": "B - negative price", "price_vat_excl": -50,
         "stocks": {"praha": 5}, "attributes": {}},
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
    assert ProductSyncState.objects.filter(sku="SKU-A").exists()
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
    path.write_text(json.dumps([{"id": "SKU-A", "title": "A", "price_vat_excl": 100,
                                 "stocks": {"praha": 10}, "attributes": {"color": "red"}}]))
    SyncService(client=mock_client, data_path=path).run()
    mock_client.reset_mock()

    path.write_text(json.dumps([{"id": "SKU-A", "title": "A", "price_vat_excl": 200,
                                 "stocks": {"praha": 10}, "attributes": {"color": "red"}}]))
    stats = SyncService(client=mock_client, data_path=path).run()

    assert stats["updated"] == 1
    assert stats["created"] == 0
    mock_client.update_product.assert_called_once()
    args, _ = mock_client.update_product.call_args
    assert args[0] == "SKU-A"


@pytest.mark.django_db
def test_previously_quarantined_sku_is_resolved_when_data_becomes_valid(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([{"id": "SKU-A", "title": "A", "price_vat_excl": -10,
                                 "stocks": {"praha": 1}, "attributes": {}}]))
    SyncService(client=mock_client, data_path=path).run()
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).exists()

    path.write_text(json.dumps([{"id": "SKU-A", "title": "A", "price_vat_excl": 100,
                                 "stocks": {"praha": 1}, "attributes": {}}]))
    stats = SyncService(client=mock_client, data_path=path).run()

    assert stats["created"] == 1
    assert stats["resolved"] == 1
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).count() == 0


@pytest.mark.django_db
def test_repeated_invalid_sku_refreshes_existing_open_quarantine(tmp_path, mock_client):
    path = tmp_path / "erp.json"
    path.write_text(json.dumps([{"id": "SKU-A", "title": "A", "price_vat_excl": -10,
                                 "stocks": {"praha": 1}, "attributes": {}}]))
    SyncService(client=mock_client, data_path=path).run()
    open_record = QuarantinedProduct.objects.get(sku="SKU-A", resolved_at__isnull=True)

    path.write_text(json.dumps([{"id": "SKU-A", "title": "", "price_vat_excl": 100,
                                 "stocks": {"praha": 1}, "attributes": {}}]))
    stats = SyncService(client=mock_client, data_path=path).run()

    refreshed = QuarantinedProduct.objects.get(sku="SKU-A", resolved_at__isnull=True)
    assert stats["quarantined"] == 1
    assert QuarantinedProduct.objects.filter(sku="SKU-A", resolved_at__isnull=True).count() == 1
    assert refreshed.pk == open_record.pk
    assert refreshed.reason == "missing_title"
    assert refreshed.raw_payload["title"] == ""


def test_stable_hash_is_deterministic_and_order_independent():
    a = {"sku": "x", "title": "t", "color": "r", "total_stock": 1, "price_with_vat": "1.00"}
    b = {"price_with_vat": "1.00", "color": "r", "total_stock": 1, "title": "t", "sku": "x"}
    assert stable_hash(a) == stable_hash(b)


def test_stable_hash_differs_on_change():
    a = {"sku": "x", "price_with_vat": "1.00"}
    b = {"sku": "x", "price_with_vat": "2.00"}
    assert stable_hash(a) != stable_hash(b)
