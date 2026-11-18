"""Counters for /health (R5.14) that are not the Store's own."""
from __future__ import annotations

import threading
from typing import Optional


class Stats:
    def __init__(self, clock):
        self.clock = clock
        self._lock = threading.Lock()
        self.rejected = {"proto": 0, "group": 0, "identity": 0, "overflow": 0}
        self.peers_discovered = 0
        self.peers_live = 0
        self._last_gossip_at: Optional[float] = None

    def count(self, kind: str) -> None:
        with self._lock:
            self.rejected[kind] += 1

    def gossip_ok(self) -> None:
        """A successful exchange in either direction."""
        with self._lock:
            self._last_gossip_at = self.clock.monotonic()

    def set_peers(self, discovered: int, live: int) -> None:
        with self._lock:
            self.peers_discovered, self.peers_live = discovered, live

    def snapshot(self, store_overflow: int = 0) -> dict:
        with self._lock:
            age = None if self._last_gossip_at is None else round(self.clock.monotonic() - self._last_gossip_at, 1)
            rejected = dict(self.rejected)
            rejected["overflow"] += store_overflow
            return {"peers_discovered": self.peers_discovered, "peers_live": self.peers_live,
                    "last_gossip_age_s": age, "rejected": rejected}
