from django.db import models
from django.db.models import Q


class ProductSyncState(models.Model):
    """Last known state per SKU — used to compute delta vs. current ERP payload."""

    sku = models.CharField(max_length=64, primary_key=True)
    payload_hash = models.CharField(max_length=64, null=True, blank=True)
    remote_exists = models.BooleanField(default=False)
    create_in_progress = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_remote_status = models.SmallIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return self.sku


class QuarantinedProduct(models.Model):
    """ERP records rejected by validation — surfaced for human review."""

    sku = models.CharField(max_length=64)
    raw_payload = models.JSONField()
    reason = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["sku", "resolved_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["sku"],
                condition=Q(resolved_at__isnull=True),
                name="unique_open_quarantine_per_sku",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku} ({self.reason})"
