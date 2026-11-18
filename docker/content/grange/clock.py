"""Clocks. Every module takes one, so tests and the simulation run in simulated time.

`monotonic()` measures ages and intervals; it never jumps backwards. `wall()`
is seconds since the epoch and is used only to choose an incarnation (R6.17,
R4.7): nothing compares wall clocks across machines.
"""
from __future__ import annotations

import threading
import time


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock:
    """A clock that moves only when told to. Thread-safe."""

    def __init__(self, monotonic: float = 1000.0, wall: float = 1_763_974_800.0):
        self._lock = threading.Lock()
        self._mono = float(monotonic)
        self._wall = float(wall)

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def wall(self) -> float:
        with self._lock:
            return self._wall

    def advance(self, seconds: float) -> None:
        """Both readings move forward together, as on a machine that is awake."""
        with self._lock:
            self._mono += seconds
            self._wall += seconds

    def set_wall(self, wall: float) -> None:
        """Step the wall clock alone, as NTP does."""
        with self._lock:
            self._wall = float(wall)
