from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("integrator", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productsyncstate",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="productsyncstate",
            name="payload_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="productsyncstate",
            name="create_in_progress",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="productsyncstate",
            name="remote_exists",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="quarantinedproduct",
            constraint=models.UniqueConstraint(
                condition=Q(resolved_at__isnull=True),
                fields=("sku",),
                name="unique_open_quarantine_per_sku",
            ),
        ),
    ]
