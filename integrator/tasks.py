import logging

from celery import shared_task

from .eshop_client import EshopError
from .sync_service import SyncService

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="integrator.tasks.sync_erp_to_eshop",
    # SyncService catches EshopError per-product, so it normally never escapes.
    # Listed here as a safety net: if a future refactor lets it propagate (or a
    # global failure mode appears), Celery retries with backoff instead of
    # silently dropping the tick.
    autoretry_for=(EshopError,),
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def sync_erp_to_eshop(self) -> dict:
    """Run a full ERP -> e-shop sync cycle. Returns a stats summary."""
    stats = SyncService().run()
    logger.info("sync_erp_to_eshop result: %s", stats)
    return stats
