import logging

from celery import shared_task

from .sync_service import SyncService

logger = logging.getLogger(__name__)


@shared_task(
    name="integrator.tasks.sync_erp_to_eshop",
)
def sync_erp_to_eshop() -> dict:
    """Run a full ERP -> e-shop sync cycle. Returns a stats summary."""
    stats = SyncService().run()
    if stats["failed"] > 0:
        logger.error("sync_erp_to_eshop finished with failures: %s", stats)
    else:
        logger.info("sync_erp_to_eshop result: %s", stats)
    return stats
