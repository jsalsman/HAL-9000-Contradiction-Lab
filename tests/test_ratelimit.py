"""Per-IP rate limiter."""

from hal.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_limit_and_window():
    clock = Clock()
    limiter = RateLimiter(2, 10, clock)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    clock.now = 10
    assert limiter.allow("a")


def test_idle_addresses_are_evicted_past_the_bound():
    clock = Clock()
    limiter = RateLimiter(5, 10, clock, max_addresses=5)
    for index in range(5):
        limiter.allow(f"idle-{index}")
    assert len(limiter._events) == 5
    clock.now = 20
    limiter.allow("fresh")
    # Every idle bucket expired and was dropped; only the new address remains.
    assert list(limiter._events) == ["fresh"]


def test_live_addresses_are_hard_capped():
    clock = Clock()
    limiter = RateLimiter(5, 10, clock, max_addresses=5)
    # A flood of distinct addresses within one window: nothing has expired yet.
    for index in range(50):
        clock.now = index * 0.01
        assert limiter.allow(f"flood-{index}")
    assert len(limiter._events) == 5
    # The least recently seen buckets were evicted; the newest remain.
    assert list(limiter._events) == [f"flood-{index}" for index in range(45, 50)]
