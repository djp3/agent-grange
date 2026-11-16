"""Minimal JSON web server that runs INSIDE the container.

It listens on 0.0.0.0 so Docker's published port can reach it. Binding to
127.0.0.1 here would make the service unreachable from the host even with
the port published.

While running, a background thread polls the tag every RECHECK_SECONDS
(default 30) using a conditional request: a 304 means the tag has not moved
and costs nothing against GitHub's rate limit, so only a real change triggers
the full comparison in freshness.py. If the image has fallen behind the tag,
every route (including /health) answers 503 until a rebuilt container
replaces this one. Docker's healthcheck then marks the container unhealthy,
and the discovery client stops selecting it.
"""
import json
import os
import random
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import freshness

PORT = int(os.environ.get("PORT", "8080"))
RECHECK_SECONDS = float(os.environ.get("RECHECK_SECONDS", "30"))
STARTED = time.time()
REQUESTS = 0
CFG = freshness.Config.from_env()
BUILD = {
    "repo": CFG.repo,
    "branch": os.environ.get("GIT_BRANCH", "unknown"),
    "commit": CFG.built,
    "dirty": CFG.dirty,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FreshnessState:
    """Latest verdict from the periodic re-check. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self.stale: freshness.Result | None = None
        self.last_polled: str | None = None
        self.last_summary = f"verified at startup by entrypoint.py; first poll in {RECHECK_SECONDS:.0f}s"
        self.polls = 0
        self.unchanged_polls = 0          # consecutive 304s: free, and nothing moved

    def update(self, res: freshness.Result, changed: bool) -> tuple[bool, bool]:
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
            return was, self.stale is not None

    def snapshot(self):
        with self._lock:
            info = {"last_polled": self.last_polled, "last_summary": self.last_summary,
                    "recheck_seconds": RECHECK_SECONDS, "polls": self.polls,
                    "consecutive_unchanged_polls": self.unchanged_polls}
            return self.stale, info


STATE = FreshnessState()


def recheck_loop() -> None:
    checker = freshness.Checker(CFG)
    while True:
        # Jitter so a whole team does not poll GitHub in lockstep.
        time.sleep(RECHECK_SECONDS * random.uniform(0.9, 1.1))
        try:
            res, changed = checker.poll()
        except Exception as e:                 # never let the checker thread die
            log(f"freshness re-check: unexpected error: {e!r}")
            continue
        was, now = STATE.update(res, changed)
        if now and not was:
            log(f"freshness re-check: IMAGE IS OUT OF DATE, refusing all requests: {res.summary}. Fix: {freshness.FIX}")
        elif was and not now:
            log(f"freshness re-check: image is current again, serving: {res.summary}")
        elif res.verdict is freshness.Verdict.UNVERIFIABLE:
            log(f"freshness re-check: warning: {res.summary} (keeping previous state)")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global REQUESTS
        REQUESTS += 1
        stale, info = STATE.snapshot()
        if stale is not None:
            # Out of date: refuse every route, /health included, so Docker marks us unhealthy.
            return self._send(503, {
                "error": "image out of date",
                "summary": stale.summary,
                "details": stale.details,
                "built_commit": CFG.built,
                "tag": CFG.tag,
                "tag_commit": stale.tagged,
                "fix": freshness.FIX,
                "checked": info["last_polled"],
            }, close=True)

        if self.path == "/health":
            body = {"status": "ok", "freshness": info}
        elif self.path == "/version":
            body = BUILD
        else:
            body = {
                "message": "hello from inside the container",
                "path": self.path,
                "container_hostname": socket.gethostname(),
                "server_time": now_iso(),
                "uptime_seconds": round(time.time() - STARTED, 1),
                "requests_served": REQUESTS,
                "build": BUILD,
                "freshness": info,
            }
        self._send(200, body)

    def _send(self, code: int, body: dict, close: bool = False) -> None:
        payload = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if close:
            self.send_header("Connection", "close")
            self.send_header("Retry-After", "60")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # Quieter container logs; healthchecks hit /health every few seconds.
        if self.path != "/health":
            super().log_message(fmt, *args)


if __name__ == "__main__":
    if CFG.skip:
        log("periodic freshness re-check disabled (SKIP_FRESHNESS_CHECK=1)")
    else:
        threading.Thread(target=recheck_loop, name="freshness-recheck", daemon=True).start()
        log(f"periodic freshness re-check every {RECHECK_SECONDS:.0f}s against tag '{CFG.tag}' "
            f"(conditional requests: unchanged polls are free)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on 0.0.0.0:{PORT}")
    server.serve_forever()
