"""The thin shell around the gossip core: timers, the worker pool, the HTTP
transport, and the shutdown sequence (plan phase 3).

  worker pool    2 x FANOUT threads (R6.20)
  transport      HTTP POST /v1/gossip; connect times out after
                 CONNECT_TIMEOUT_SECONDS, a whole exchange after
                 EXCHANGE_TIMEOUT_SECONDS; anything but a 2xx is a failure
  scheduler      one thread that calls core.tick() at its deadlines
  startup        core.start(), then waits at most 5 s for the repairs (6.1)
  shutdown       core.leave(), then waits at most 2 s for the pushes (R6.16)
"""
from __future__ import annotations

import http.client
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .api_gossip import parse_reply
from .config import LEAVE_WAIT_SECONDS, MAX_GOSSIP_BYTES, STARTUP_REPAIR_SECONDS
from .gossip import Result
from .model import encode_json


class HttpTransport:
    def __init__(self, cfg, pool: ThreadPoolExecutor):
        self.cfg = cfg
        self.pool = pool

    def send(self, addr: str, msg, expect_reply: bool, done: Callable[[Result], None]) -> None:
        payload = encode_json(msg.to_json())
        try:
            self.pool.submit(self._run, addr, payload, expect_reply, done)
        except RuntimeError:                         # pool shut down
            done(Result(False))

    def _run(self, addr, payload, expect_reply, done) -> None:
        try:
            result = self.exchange(addr, payload, expect_reply)
        except Exception:                            # never let a worker die silently
            result = Result(False)
        done(result)

    def exchange(self, addr: str, payload: bytes, expect_reply: bool) -> Result:
        host, _, port = addr.rpartition(":")
        deadline = time.monotonic() + self.cfg.exchange_timeout_s
        conn = http.client.HTTPConnection(host, int(port), timeout=self.cfg.connect_timeout_s)
        try:
            conn.connect()
            conn.sock.settimeout(max(0.05, deadline - time.monotonic()))
            conn.request("POST", "/v1/gossip", body=payload,
                         headers={"Content-Type": "application/json", "Connection": "close"})
            resp = conn.getresponse()
            declared = resp.getheader("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > MAX_GOSSIP_BYTES:
                return Result(False, resp.status)
            data = resp.read(MAX_GOSSIP_BYTES + 1)
            status = resp.status
        except (OSError, http.client.HTTPException, ValueError):
            return Result(False)
        finally:
            conn.close()
        if not 200 <= status < 300 or time.monotonic() > deadline:
            return Result(False, status)
        if not expect_reply:
            return Result(True, status)
        if len(data) > MAX_GOSSIP_BYTES:
            return Result(False, status)
        reply = parse_reply(data, self.cfg)
        return Result(reply is not None, status, reply)


class Scheduler:
    """Calls core.tick() at the deadlines it returns."""

    def __init__(self, core, clock, log):
        self.core = core
        self.clock = clock
        self.log = log
        self._stop = threading.Event()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                deadline = self.core.tick()
            except Exception as e:
                self.log(f"scheduler: unexpected error: {e!r}")
                deadline = self.clock.monotonic() + 1.0
            self._stop.wait(min(1.0, max(0.01, deadline - self.clock.monotonic())))

    def start(self) -> "Scheduler":
        threading.Thread(target=self._loop, name="gossip-scheduler", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


def wait_until(predicate, seconds: float, step: float = 0.02) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def startup(core, log) -> None:
    """6.1 step 5, bounded to 5 seconds in total."""
    sent = core.start()
    if sent:
        wait_until(lambda: core.startup_pending <= 0, STARTUP_REPAIR_SECONDS)
        log(f"startup: {core.startup_ok} of {sent} repair(s) answered; "
            f"{len(core.store.peer_ids())} grange(s) known")


def shutdown(core, log) -> None:
    """R6.16: leaving body to all neighbours, at most two seconds for the pushes."""
    pushes = core.leave()
    if pushes:
        drained = wait_until(lambda: core.outstanding <= 0, LEAVE_WAIT_SECONDS)
        log(f"leaving: pushed to {pushes} neighbour(s){'' if drained else ', some unconfirmed'}")


class Resolver:
    """Host name to IPv4, cached; for matching an incoming source to a listed peer (R6.9)."""

    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._cache: dict = {}

    def __call__(self, host: str):
        hit = self._cache.get(host)
        now = time.monotonic()
        if hit is not None and now - hit[1] < self.ttl:
            return hit[0]
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            ip = None
        self._cache[host] = (ip, now)
        return ip
