from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from django.conf import settings

from integrator.tasks import sync_erp_to_eshop


def _stats(*, created=0, updated=0, skipped=0, invalid=0, failed=0, retryable_failed=0):
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "invalid": invalid,
        "failed": failed,
        "retryable_failed": retryable_failed,
    }


def test_sync_task_delegates_to_sync_service_run():
    stats = _stats(created=1)
    service = MagicMock()
    service.run.return_value = stats

    with patch("integrator.tasks.SyncService", return_value=service) as service_cls:
        result = sync_erp_to_eshop()

    assert result == stats
    service_cls.assert_called_once_with()
    service.run.assert_called_once_with()


def test_sync_task_retries_when_sync_service_reports_retryable_failures():
    stats = _stats(failed=1, retryable_failed=1)
    service = MagicMock()
    service.run.return_value = stats

    with patch("integrator.tasks.SyncService", return_value=service):
        with patch.object(sync_erp_to_eshop, "retry", side_effect=Retry()) as retry:
            with pytest.raises(Retry):
                sync_erp_to_eshop()

    retry.assert_called_once()
    assert retry.call_args.kwargs["countdown"] == settings.ERP_SYNC_TASK_RETRY_DELAY_SECONDS


def test_sync_task_does_not_retry_invalid_source_data():
    stats = _stats(invalid=2)
    service = MagicMock()
    service.run.return_value = stats

    with patch("integrator.tasks.SyncService", return_value=service):
        with patch.object(sync_erp_to_eshop, "retry") as retry:
            result = sync_erp_to_eshop()

    assert result == stats
    retry.assert_not_called()


def test_sync_task_acks_late_is_enabled():
    assert sync_erp_to_eshop.acks_late is True


def test_sync_task_worker_lost_semantics_are_configured():
    assert sync_erp_to_eshop.reject_on_worker_lost is True
    assert sync_erp_to_eshop.max_retries == settings.ERP_SYNC_TASK_MAX_RETRIES
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True
    assert settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
