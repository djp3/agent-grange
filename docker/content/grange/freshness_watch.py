"""The running half of the freshness check, moved here unchanged from server.py.

While running, a background thread polls the tag every RECHECK_SECONDS
(default 30) using a conditional request: a 304 means the tag has not moved
and costs nothing against GitHub's rate limit, so only a real change triggers
the full comparison in freshness.py. If the image has fallen behind the tag,
every route (including /health and /v1/gossip) answers 503 until a rebuilt
container replaces this one (R5.16, R6.18). Docker's healthcheck then marks
the container unhealthy.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

import freshness


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FreshnessState:
    """Latest verdict from the periodic re-check. Thread-safe."""

    def __init__(self, recheck_seconds: float):
        self._lock = threading.Lock()
        self.recheck_seconds = recheck_seconds
        self.stale: "freshness.Result | None" = None
        self.last_polled: "str | None" = None
        self.last_summary = f"verified at startup by entrypoint.py; first poll in {recheck_seconds:.0f}s"
        self.polls = 0
        self.unchanged_polls = 0          # consecutive 304s: free, and nothing moved
        self._listeners: list = []

    def subscribe(self, fn) -> None:
        """fn(is_stale) is called when the state flips."""
        self._listeners.append(fn)

    def update(self, res: "freshness.Result", changed: bool):
        """Apply a poll result. Returns (was_stale, is_stale)."""
        with self._lock:
            was = self.stale is not None
            if res.verdict is freshness.Verdict.STALE:
                self.stale = res
            elif res.verdict in (freshness.Verdict.FRESH, freshness.Verdict.AHEAD):
                self.stale = None
            # UNVERIFIABLE / NO_METADATA: a transient failure must not flip state either way.
            self.last_polled = now_iso()
            self.last_summary = res.summary
            self.polls += 1
            self.unchanged_polls = 0 if changed else self.unchanged_polls + 1
            now = self.stale is not None
        if was != now:
            for fn in list(self._listeners):
                fn(now)
        return was, now

    def is_stale(self) -> bool:
        with self._lock:
            return self.stale is not None

    def snapshot(self):
        with self._lock:
            info = {"last_polled": self.last_polled, "last_summary": self.last_summary,
                    "recheck_seconds": self.recheck_seconds, "polls": self.polls,
                    "consecutive_unchanged_polls": self.unchanged_polls}
            return self.stale, info


def stale_body(cfg: "freshness.Config", stale: "freshness.Result", info: dict) -> dict:
    """The 503 body every route answers with while stale."""
    return {
        "error": "image out of date",
        "summary": stale.summary,
        "details": stale.details,
        "built_commit": cfg.built,
        "tag": cfg.tag,
        "tag_commit": stale.tagged,
        "fix": freshness.FIX,
        "checked": info["last_polled"],
    }


def recheck_loop(cfg: "freshness.Config", state: FreshnessState, log=_log) -> None:
    checker = freshness.Checker(cfg)
    while True:
        # Jitter so a whole team does not poll GitHub in lockstep.
        time.sleep(state.recheck_seconds * random.uniform(0.9, 1.1))
        try:
            res, changed = checker.poll()
        except Exception as e:                 # never let the checker thread die
            log(f"freshness re-check: unexpected error: {e!r}")
            continue
        was, now = state.update(res, changed)
        if now and not was:
            log(f"freshness re-check: IMAGE IS OUT OF DATE, refusing all requests: {res.summary}. Fix: {freshness.FIX}")
        elif was and not now:
            log(f"freshness re-check: image is current again, serving: {res.summary}")
        elif res.verdict is freshness.Verdict.UNVERIFIABLE:
            log(f"freshness re-check: warning: {res.summary} (keeping previous state)")


def start(cfg: "freshness.Config", log=_log) -> FreshnessState:
    state = FreshnessState(float(os.environ.get("RECHECK_SECONDS", "30") or "30"))
    if cfg.skip:
        log("periodic freshness re-check disabled (SKIP_FRESHNESS_CHECK=1)")
    else:
        threading.Thread(target=recheck_loop, args=(cfg, state, log), name="freshness-recheck",
                         daemon=True).start()
        log(f"periodic freshness re-check every {state.recheck_seconds:.0f}s against tag '{cfg.tag}' "
            f"(conditional requests: unchanged polls are free)")
    return state
