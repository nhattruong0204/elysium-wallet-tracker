"""Small in-process TTL cache with single-flight computation and stale-on-error.

One uvicorn worker, so a dict + locks is enough. A page is computed at most once per TTL
per key no matter how many visitors ask; if the database fails the last good value is
served for `stale_s` more seconds (flagged), instead of an error page.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Hit:
    value: Any
    age_s: float
    stale: bool = False
    cached: bool = False


class TTLCache:
    def __init__(self, ttl_s: float = 30.0, max_entries: int = 256, stale_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_entries, self.stale_s, self._clock = ttl_s, max_entries, stale_s, clock
        self._data: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, threading.Lock] = {}
        self._guard = threading.Lock()
        self.hits = self.misses = self.stale_served = 0

    def _lock_for(self, key: Any) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: Any, compute: Callable[[], Any]) -> Hit:
        now = self._clock()
        ent = self._data.get(key)
        if ent and now - ent[0] < self.ttl:
            self.hits += 1
            return Hit(ent[1], now - ent[0], cached=True)
        with self._lock_for(key):
            now = self._clock()
            ent = self._data.get(key)
            if ent and now - ent[0] < self.ttl:      # another thread just filled it
                self.hits += 1
                return Hit(ent[1], now - ent[0], cached=True)
            try:
                value = compute()
            except Exception:
                if ent and now - ent[0] < self.ttl + self.stale_s:
                    self.stale_served += 1
                    return Hit(ent[1], now - ent[0], stale=True, cached=True)
                raise
            self.misses += 1
            self._store(key, value)
            return Hit(value, 0.0)

    def _store(self, key: Any, value: Any) -> None:
        with self._guard:
            self._data[key] = (self._clock(), value)
            if len(self._data) > self.max_entries:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[: len(self._data) - self.max_entries]:
                    self._data.pop(k, None)
                    self._locks.pop(k, None)

    def clear(self) -> None:
        with self._guard:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
