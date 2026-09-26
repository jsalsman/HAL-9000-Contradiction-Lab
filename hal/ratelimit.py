"""Per-IP sliding-window rate limits for run starts and viewer flags.

Limits are held in process memory. With max-instances=1 they are exact; with
several instances each enforces its own window, so the effective limit scales
with the instance count. IP addresses are never persisted or logged.
"""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Allow at most ``limit`` events per ``window`` seconds per client address."""

    def __init__(self, limit: int, window: float, clock=time.monotonic) -> None:
        """Configure the limit, window, and (for tests) a clock."""
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, address: str) -> bool:
        """Record and allow an event, or return False when over the limit."""
        now = self.clock()
        with self._lock:
            events = self._events[address]
            # Drop events that have left the window.
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            # Bound memory: forget idle addresses opportunistically.
            if len(self._events) > 10_000:
                for key in [k for k, v in self._events.items() if not v]:
                    del self._events[key]
            return True
