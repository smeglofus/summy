from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProductSyncState",
            fields=[
                ("sku", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("payload_hash", models.CharField(blank=True, max_length=64, null=True)),
                ("remote_exists", models.BooleanField(default=False)),
                ("create_in_progress", models.BooleanField(default=False)),
                ("pending_payload_hash", models.CharField(blank=True, max_length=64, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_remote_status", models.SmallIntegerField(blank=True, null=True)),
            ],
        ),
    ]
