import threading

import pytest
from django.test import override_settings

from integrator import rate_limiter as rate_limiter_module
from integrator.rate_limiter import RateLimiter, _IN_PROCESS_WINDOWS


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FailingRedis:
    def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.fixture(autouse=True)
def clear_in_process_windows():
    _IN_PROCESS_WINDOWS.clear()
    yield
    _IN_PROCESS_WINDOWS.clear()


def test_first_n_acquires_do_not_sleep():
    clock = FakeClock()
    limiter = RateLimiter(rate=5, per=1.0, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    for _ in range(5):
        limiter.acquire()
    assert clock.sleeps == []


def test_in_process_fallback_is_shared_by_cache_key():
    clock = FakeClock()
    limiter_a = RateLimiter(
        rate=1,
        per=1.0,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        use_redis=False,
        cache_key="test:shared",
    )
    limiter_b = RateLimiter(
        rate=1,
        per=1.0,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        use_redis=False,
        cache_key="test:shared",
    )

    limiter_a.acquire()
    clock.now = 0.25
    limiter_b.acquire()

    assert clock.sleeps == [0.75]


def test_sixth_acquire_sleeps_for_remaining_window():
    clock = FakeClock()
    limiter = RateLimiter(rate=5, per=1.0, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    for _ in range(5):
        limiter.acquire()
    clock.now = 0.4  # 0.4s after the first acquire
    limiter.acquire()
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == 0.6  # waits the remainder of the 1s window


def test_no_sleep_when_window_already_elapsed():
    clock = FakeClock()
    limiter = RateLimiter(rate=2, per=1.0, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.acquire()
    limiter.acquire()
    clock.now = 1.5
    limiter.acquire()
    assert clock.sleeps == []


def test_sleeping_acquire_does_not_hold_lock():
    clock = FakeClock()
    sleep_started = threading.Event()
    release_sleep = threading.Event()

    def controlled_sleep(seconds: float) -> None:
        sleep_started.set()
        release_sleep.wait(timeout=1.0)
        clock.now += seconds

    limiter = RateLimiter(
        rate=1,
        per=1.0,
        sleep_fn=controlled_sleep,
        monotonic_fn=clock.monotonic,
    )
    limiter.acquire()

    thread = threading.Thread(target=limiter.acquire)
    thread.start()
    try:
        assert sleep_started.wait(timeout=1.0)
        acquired = limiter._lock.acquire(blocking=False)
        if acquired:
            limiter._lock.release()
    finally:
        release_sleep.set()
        thread.join(timeout=1.0)

    assert acquired is True
    assert not thread.is_alive()


def test_redis_failure_falls_back_to_in_process_limiter():
    clock = FakeClock()
    limiter = RateLimiter(
        rate=1,
        per=1.0,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        redis_client=FailingRedis(),
    )

    limiter.acquire()
    limiter.acquire()

    assert limiter.using_redis is False
    assert clock.sleeps == [1.0]


@override_settings(CACHES={
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis.example:6379/2",
    }
})
def test_default_redis_cache_location_is_used_when_available(monkeypatch):
    redis_client = object()
    calls = []

    def fake_from_url(url):
        calls.append(url)
        return redis_client

    monkeypatch.setattr(rate_limiter_module.Redis, "from_url", fake_from_url)

    limiter = RateLimiter(rate=5, per=1.0)

    assert limiter.using_redis is True
    assert limiter._redis_client is redis_client
    assert calls == ["redis://redis.example:6379/2"]


def test_default_test_cache_uses_in_process_limiter():
    limiter = RateLimiter(rate=5, per=1.0)

    assert limiter.using_redis is False
