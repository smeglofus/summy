import logging

from celery import shared_task
from django.conf import settings

from .sync_service import SyncService

logger = logging.getLogger(__name__)


class SyncRetryableFailure(Exception):
    """Raised to make Celery retry a sync run with transient API failures."""

    def __init__(self, stats: dict):
        self.stats = stats
        super().__init__(f"ERP sync has retryable failures: {stats}")


@shared_task(
    bind=True,
    name="integrator.tasks.sync_erp_to_eshop",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=settings.ERP_SYNC_TASK_MAX_RETRIES,
    default_retry_delay=settings.ERP_SYNC_TASK_RETRY_DELAY_SECONDS,
)
def sync_erp_to_eshop(self) -> dict:
    """Run a full ERP -> e-shop sync cycle. Returns a stats summary."""
    stats = SyncService().run()
    if stats.get("retryable_failed", 0) > 0:
        logger.error("sync_erp_to_eshop finished with retryable failures: %s", stats)
        raise self.retry(
            exc=SyncRetryableFailure(stats),
            countdown=settings.ERP_SYNC_TASK_RETRY_DELAY_SECONDS,
        )
    if stats.get("failed", 0) > 0 or stats.get("invalid", 0) > 0:
        logger.warning("sync_erp_to_eshop finished with non-retryable issues: %s", stats)
    else:
        logger.info("sync_erp_to_eshop result: %s", stats)
    return stats
