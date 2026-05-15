"""Singleton sync lock.

Production uses a PostgreSQL advisory lock bound to the current DB connection:
no TTL, ownership-safe release, and automatic cleanup if the worker dies.

Tests / non-Postgres environments fall back to a best-effort cache lock because
SQLite has no equivalent advisory primitive.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from django.core.cache import cache
from django.db import connection

SYNC_LOCK_KEY = "integrator:erp-sync"
SYNC_LOCK_ID = 2_025_051_501


@dataclass
class SyncRunLock:
    token: str | None = None
    _backend: str | None = None

    def acquire(self) -> bool:
        if connection.vendor == "postgresql":
            acquired = self._acquire_postgres()
            if acquired:
                self._backend = "postgresql"
            return acquired

        token = uuid4().hex
        if not cache.add(SYNC_LOCK_KEY, token, timeout=None):
            return False
        self.token = token
        self._backend = "cache"
        return True

    def release(self) -> None:
        if self._backend == "postgresql":
            self._release_postgres()
            self._backend = None
            return

        if self._backend == "cache" and self.token is not None:
            if cache.get(SYNC_LOCK_KEY) == self.token:
                cache.delete(SYNC_LOCK_KEY)
            self.token = None
            self._backend = None

    @staticmethod
    def _acquire_postgres() -> bool:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [SYNC_LOCK_ID])
            return bool(cursor.fetchone()[0])

    @staticmethod
    def _release_postgres() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [SYNC_LOCK_ID])
