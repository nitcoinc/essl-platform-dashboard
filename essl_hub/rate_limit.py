"""In-memory sliding-window rate limit (the fix report form's POST limits)."""

from __future__ import annotations

import threading
import time as _clock
from collections import deque


class SlidingWindow:
    """At most ``limit`` hits per key in any ``seconds`` span, in memory.

    The web runs one gunicorn worker, so one process sees every POST; a
    restart forgets the counts, which only ever loosens the limit."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = _clock.monotonic()
        cutoff = now - self.seconds
        with self._lock:
            if len(self._hits) > 10_000:
                for stale in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
                    del self._hits[stale]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
