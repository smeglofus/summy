from django.db import models


class ProductSyncState(models.Model):
    """Last known state per SKU, used to compute delta vs. current ERP payload."""

    sku = models.CharField(max_length=64, primary_key=True)
    payload_hash = models.CharField(max_length=64, null=True, blank=True)
    remote_exists = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_remote_status = models.SmallIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return self.sku
