from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProductSyncState",
            fields=[
                ("sku", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("payload_hash", models.CharField(max_length=64)),
                ("last_synced_at", models.DateTimeField(auto_now=True)),
                ("last_remote_status", models.SmallIntegerField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="QuarantinedProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("sku", models.CharField(max_length=64)),
                ("raw_payload", models.JSONField()),
                ("reason", models.CharField(max_length=64)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["sku", "resolved_at"],
                        name="integrator__sku_a0e1c2_idx",
                    )
                ],
            },
        ),
    ]
