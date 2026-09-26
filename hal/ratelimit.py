"""Per-IP sliding-window rate limits for run starts and viewer flags.

Limits are held in process memory. The service runs with max-instances=1 and
one Gunicorn worker, so each limit is exact for the whole service. IP addresses
are never persisted or logged.
"""

import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    """Allow at most ``limit`` events per ``window`` seconds per client address."""

    def __init__(
        self, limit: int, window: float, clock=time.monotonic, max_addresses: int = 10_000
    ) -> None:
        """Configure the limit, window, address bound, and (for tests) a clock."""
        self.limit, self.window, self.clock = limit, window, clock
        self.max_addresses = max_addresses
        # Least recently seen address first, so eviction drops the stalest bucket.
        self._events: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()
        self._next_sweep = 0.0

    def _sweep(self, now: float) -> None:
        """Drop expired events from every bucket and delete empty buckets.

        An idle address is otherwise pruned only when it calls again, so its
        expired entries would keep the map growing without bound.
        """
        for address in list(self._events):
            events = self._events[address]
            while events and now - events[0] >= self.window:
                events.popleft()
            if not events:
                del self._events[address]

    def allow(self, address: str) -> bool:
        """Record and allow an event, or return False when over the limit."""
        now = self.clock()
        with self._lock:
            events = self._events.setdefault(address, deque())
            self._events.move_to_end(address)
            # Drop events that have left the window.
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            # Bound memory: past the address bound, sweep expired buckets at most
            # once per 1/60 of the window, so the sweep's cost stays amortized.
            if len(self._events) > self.max_addresses and now >= self._next_sweep:
                self._sweep(now)
                self._next_sweep = now + self.window / 60
            # Hard cap: if live buckets still exceed the bound (a flood of distinct
            # addresses within one window), evict the least recently seen ones.
            while len(self._events) > self.max_addresses:
                self._events.popitem(last=False)
            return True
