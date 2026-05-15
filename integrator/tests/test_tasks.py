from unittest.mock import MagicMock, patch

from integrator.tasks import sync_erp_to_eshop


def test_sync_task_delegates_to_sync_service_run():
    stats = {"created": 1, "updated": 0, "skipped": 0, "failed": 0}
    service = MagicMock()
    service.run.return_value = stats

    with patch("integrator.tasks.SyncService", return_value=service) as service_cls:
        result = sync_erp_to_eshop()

    assert result == stats
    service_cls.assert_called_once_with()
    service.run.assert_called_once_with()
