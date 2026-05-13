"""Synchronous ERP -> e-shop sync — handy for local debugging."""
import json

from django.core.management.base import BaseCommand

from integrator.sync_service import SyncService


class Command(BaseCommand):
    help = "Run a single ERP -> e-shop sync cycle in-process (no Celery)."

    def handle(self, *args, **options):
        stats = SyncService().run()
        self.stdout.write(json.dumps(stats, indent=2))
