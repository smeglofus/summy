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
