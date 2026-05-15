"""Sliding-window rate limiter for outbound API calls.

Uses Redis through Django's default cache when that backend exposes a Redis
client. Tests and local runs without Redis fall back to the in-process limiter.
"""
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from redis import Redis

logger = logging.getLogger(__name__)


@dataclass
class _InProcessWindow:
    timestamps: deque[float]
    lock: threading.Lock = field(default_factory=threading.Lock)


_IN_PROCESS_WINDOWS: dict[tuple[str, int, int], _InProcessWindow] = {}
_IN_PROCESS_WINDOWS_GUARD = threading.Lock()

_REDIS_SCRIPT = """
local key = KEYS[1]
local per_ms = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local member = ARGV[3]
local expire_ms = tonumber(ARGV[4])

local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
redis.call("ZREMRANGEBYSCORE", key, 0, now_ms - per_ms)

if redis.call("ZCARD", key) < rate then
    redis.call("ZADD", key, now_ms, member)
    redis.call("PEXPIRE", key, expire_ms)
    return {1, 0}
end

local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
local wait_ms = per_ms
if oldest[2] then
    wait_ms = per_ms - (now_ms - tonumber(oldest[2]))
    if wait_ms < 0 then
        wait_ms = 0
    end
end
return {0, wait_ms}
"""


def _get_redis_url() -> str | None:
    cache_config = settings.CACHES.get("default", {})
    if cache_config.get("BACKEND") != "django.core.cache.backends.redis.RedisCache":
        return None
    location = cache_config.get("LOCATION")
    if isinstance(location, (list, tuple)):
        return location[0] if location else None
    return location


def _get_redis_client(redis_url: str | None = None):
    url = redis_url or _get_redis_url()
    if not url:
        return None
    try:
        return Redis.from_url(url)
    except Exception as exc:  # pragma: no cover - backend-specific setup failure
        logger.warning("Redis rate limiter unavailable, using in-process fallback: %s", exc)
        return None


def _get_in_process_window(cache_key: str, rate: int, per: float) -> _InProcessWindow:
    per_ms = max(int(per * 1000), 1)
    key = (cache_key, rate, per_ms)
    with _IN_PROCESS_WINDOWS_GUARD:
        window = _IN_PROCESS_WINDOWS.get(key)
        if window is None:
            window = _InProcessWindow(timestamps=deque(maxlen=rate))
            _IN_PROCESS_WINDOWS[key] = window
        return window


class RateLimiter:
    def __init__(
        self,
        rate: int,
        per: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        redis_client=None,
        redis_url: str | None = None,
        cache_key: str = "eshop:rate-limit:products",
        use_redis: bool = True,
    ):
        if rate < 1:
            raise ValueError("rate must be >= 1")
        if per <= 0:
            raise ValueError("per must be > 0")
        self.rate = rate
        self.per = per
        self.cache_key = cache_key
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._redis_client = redis_client if redis_client is not None else (
            _get_redis_client(redis_url) if use_redis else None
        )
        self._redis_error_logged = False
        self._window = _get_in_process_window(cache_key, rate, per)
        self._timestamps = self._window.timestamps
        self._lock = self._window.lock

    @property
    def using_redis(self) -> bool:
        return self._redis_client is not None

    def acquire(self) -> None:
        """Block until a request slot is free."""
        if self._redis_client is not None:
            try:
                self._acquire_redis()
                return
            except Exception as exc:
                self._redis_client = None
                if not self._redis_error_logged:
                    logger.warning(
                        "Redis rate limiter failed, using in-process fallback: %s",
                        exc,
                    )
                    self._redis_error_logged = True

        self._acquire_in_process()

    def _acquire_redis(self) -> None:
        per_ms = max(int(self.per * 1000), 1)
        expire_ms = max(per_ms * 2, 1000)
        while True:
            allowed, wait_ms = self._redis_client.eval(
                _REDIS_SCRIPT,
                1,
                self.cache_key,
                per_ms,
                self.rate,
                uuid.uuid4().hex,
                expire_ms,
            )
            if int(allowed) == 1:
                return
            wait = max(int(wait_ms), 0) / 1000
            if wait > 0:
                self._sleep(wait)

    def _acquire_in_process(self) -> None:
        while True:
            with self._lock:
                now = self._monotonic()

                while self._timestamps and now - self._timestamps[0] >= self.per:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.rate:
                    self._timestamps.append(now)
                    return

                oldest = self._timestamps[0]
                wait = self.per - (now - oldest)

            if wait > 0:
                self._sleep(wait)
