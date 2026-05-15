import threading

from integrator.rate_limiter import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_first_n_acquires_do_not_sleep():
    clock = FakeClock()
    limiter = RateLimiter(rate=5, per=1.0, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    for _ in range(5):
        limiter.acquire()
    assert clock.sleeps == []


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
