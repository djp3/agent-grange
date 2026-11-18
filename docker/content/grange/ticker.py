"""Time-driven transitions, once a second (plan phase 2).

Each run: expire peers (R6.4), notice a resume (R6.22), re-evaluate the local
farm_stale (R6.3). A resume is a gap since the previous run longer than
RESUME_GAP_SECONDS on the monotonic clock: the process was paused or the
machine slept with the clock running on. The gossip core acts on it.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

from .config import RESUME_GAP_SECONDS


class Ticker:
    def __init__(self, store, clock, on_resume: Optional[Callable[[float], None]] = None,
                 interval: float = 1.0, gap: float = RESUME_GAP_SECONDS, log=None):
        self.store = store
        self.clock = clock
        self.on_resume = on_resume
        self.interval = interval
        self.gap = gap
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.last_run = clock.monotonic()
        self.resumes = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> bool:
        """One run. Returns True if it saw a resume gap."""
        now = self.clock.monotonic()
        gap = now - self.last_run
        self.last_run = now
        self.store.expire()
        resumed = gap > self.gap
        if resumed:
            self.resumes += 1
            self.log(f"resume: {gap:.1f}s since the previous tick; bumping and repairing now")
            if self.on_resume is not None:
                self.on_resume(gap)
        self.store.evaluate_farm_stale()
        return resumed

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as e:             # the ticker must not die
                self.log(f"ticker: unexpected error: {e!r}")

    def start(self) -> "Ticker":
        self.last_run = self.clock.monotonic()
        self._thread = threading.Thread(target=self._loop, name="ticker", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
