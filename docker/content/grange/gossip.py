"""The gossip core: every decision about who is talked to, and what is sent.

No threads and no sockets. It is driven by the clock object (`tick()`), by
the Store's events, by the ticker's resume signal, and by the results its
transport reports. The runtime (runtime.py) and the simulation (tests/sim)
use it unchanged, so the simulation exercises the real behaviour (R10.5).

Rules implemented here:
  R6.2   heartbeat: bump every HEARTBEAT_SECONDS, nothing sent
  R6.8   discovered set refreshed every DISCOVERY_SECONDS
  R6.10  live set, neighbours (ring successor plus hash-ranked picks), probe,
         choice of repair peer
  R6.11  own content changes and corrections pushed at once to all neighbours
  R6.12  new bodies forwarded once to all neighbours except the sender
  R6.13  one repair every REPAIR_SECONDS
  R6.16  leaving
  R6.19  address-to-id binding from the most recent successful exchange
  R6.20  failed marks
  R6.22  resume: bump and repair at once
  R5.16  nothing is sent while the image is stale
  6.1    startup: discovery, then up to FANOUT parallel repairs
"""
from __future__ import annotations

import hashlib
import random
import threading
from typing import Callable, NamedTuple, Optional

from .model import GossipMessage
from .state import FORWARD


class Result(NamedTuple):
    """What a transport reports for one request. `ok` means a 2xx answer;
    anything else, a timeout, a refusal, or an error status, is a failure
    (R6.20). `response` is the parsed repair response, if one was expected."""
    ok: bool
    status: Optional[int] = None
    response: Optional[GossipMessage] = None


def hash_rank(own_id: str, addr: str) -> bytes:
    """Stable, per-grange, salt-free (Python's hash() is salted per process)."""
    return hashlib.sha256(f"{own_id}/{addr}".encode("utf-8")).digest()


def addr_host(addr: str) -> str:
    return addr.rpartition(":")[0]


def choose_neighbours(own_id: str, own_addr: str, live: list, fanout: int) -> list:
    """R6.10: the successor of our own address in sorted order, plus the
    FANOUT - 1 other live addresses with the lowest sha256 of id joined to
    address. Sorted order is the byte order of the host:port strings."""
    if not live or fanout < 1:
        return []
    ordered = sorted(live)
    successor = next((a for a in ordered if a > own_addr), ordered[0])
    others = sorted((a for a in ordered if a != successor), key=lambda a: hash_rank(own_id, a))
    return [successor] + others[:fanout - 1]


class Core:
    def __init__(self, store, cfg, clock, transport, discovery, own_address: str,
                 stats=None, rng: Optional[random.Random] = None,
                 resolve: Optional[Callable[[str], Optional[str]]] = None,
                 log: Callable[[str], None] = None):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.transport = transport
        self.discovery = discovery
        self.own = own_address
        self.stats = stats
        self.rng = rng or random.Random()
        self.resolve = resolve or (lambda host: host)
        self.log = log or (lambda m: None)
        self._lock = threading.RLock()

        self.discovered: dict = {}          # address -> online hint
        self.discovery_answered = False
        self.binding: dict = {}             # address -> grange id (R6.19)
        self.failed: set = set()            # R6.20
        self._live_key: Optional[tuple] = None
        self._neighbours: list = []
        self._source_map: dict = {}         # source IP -> topology address
        self._logged: set = set()

        self.stale = False
        self.leaving = False
        self.outstanding = 0                # requests in flight
        self.startup_pending = 0
        self.sent = {"push": 0, "repair": 0, "probe": 0}

        now = clock.monotonic()
        self.next_heartbeat = now + cfg.heartbeat_s
        self.next_repair = now + cfg.repair_s
        self.next_discovery = now + cfg.discovery_s
        store.subscribe_batch(self.on_store_events)

    # ---------------------------------------------------------------- the live set

    def _live(self) -> list:
        live_ids = self.store.peer_id_set()
        return sorted(a for a, hint in self.discovered.items()
                      if a not in self.failed and (self.binding.get(a) in live_ids or hint))

    def live(self) -> list:
        with self._lock:
            return self._live()

    def neighbours(self) -> list:
        """Recomputed only when the live set changes (R6.10)."""
        with self._lock:
            live = self._live()
            key = tuple(live)
            if key != self._live_key:
                self._live_key = key
                self._neighbours = choose_neighbours(self.store.grange_id, self.own, live, self.cfg.fanout)
                if self.stats is not None:
                    self.stats.set_peers(len(self.discovered), len(live))
            return list(self._neighbours)

    def outside(self) -> list:
        """Discovered addresses outside the live set: the probe's choices."""
        with self._lock:
            live = set(self._live())
            return sorted(a for a in self.discovered if a not in live)

    def repair_peer(self) -> Optional[str]:
        """Uniform from live non-neighbours, else from the whole live set (R6.10)."""
        nbrs = set(self.neighbours())
        live = self.live()
        if not live:
            return None
        pool = [a for a in live if a not in nbrs] or live
        return self.rng.choice(pool)

    # ---------------------------------------------------------------- discovery

    def refresh_discovery(self) -> None:
        try:
            peers = self.discovery.peers()
        except Exception as e:                       # a failed refresh keeps the last list
            self._log_once(("discovery", str(e)), f"discovery: refresh failed: {e}")
            return
        with self._lock:
            self.discovered = {a: bool(online) for a, online in peers.items() if a != self.own}
            self.discovery_answered = True
            for a, online in self.discovered.items():
                if online:
                    self.failed.discard(a)            # R6.20: the next refresh reporting it online
            self._source_map = {}
            self._live_key = None                     # recount, and recompute if the live set moved
        self.neighbours()

    def address_for_source(self, source_ip: str) -> Optional[str]:
        """The discovered address whose host resolves to an incoming request's
        source IP (R6.19, R6.9)."""
        with self._lock:
            if source_ip in self._source_map:
                return self._source_map[source_ip]
            found = None
            for a in self.discovered:
                if self.resolve(addr_host(a)) == source_ip:
                    found = a
                    break
            self._source_map[source_ip] = found
            return found

    # ------------------------------------------------------------------ binding

    def _bind(self, addr: str, gid: str) -> None:
        old = self.binding.get(addr)
        if old != gid:
            self.binding[addr] = gid
            if old is not None:
                self._log_once(("rebind", addr, gid), f"address {addr} is now grange {gid!r} (was {old!r})")
        hint = self._hostname_hint(addr)
        if hint is not None and hint != gid:
            self._log_once(("hint", addr, gid, hint), f"address {addr}: sidecar hostname says {hint!r}, "
                                                       f"records say {gid!r}; going by the records")

    def _hostname_hint(self, addr: str) -> Optional[str]:
        names = getattr(self.discovery, "hostnames", None)
        name = names.get(addr) if isinstance(names, dict) else None
        if name and name.startswith("grange-"):
            return name[len("grange-"):]
        return None

    def on_incoming(self, addr: Optional[str], sender: str) -> None:
        """An accepted request from `addr`: clears its mark, binds it (R6.19, R6.20)."""
        if addr is None:
            return
        with self._lock:
            self.failed.discard(addr)
            self._bind(addr, sender)
        if self.stats is not None:
            self.stats.gossip_ok()

    # ------------------------------------------------------------------ sending

    def _can_send(self) -> bool:
        return not self.stale

    def _send(self, addr: str, msg: GossipMessage, kind: str, on_done=None) -> None:
        with self._lock:
            self.outstanding += 1
            self.sent[kind] += 1

        def done(result: Result) -> None:
            try:
                self._on_result(addr, kind, msg, result)
                if on_done is not None:
                    on_done(result)
            finally:
                with self._lock:
                    self.outstanding -= 1

        self.transport.send(addr, msg, kind != "push", done)

    def _on_result(self, addr: str, kind: str, request: GossipMessage, result: Result) -> None:
        with self._lock:
            if not result.ok:
                self.failed.add(addr)                 # R6.20
                return
            self.failed.discard(addr)
            if result.response is not None:
                self._bind(addr, result.response.sender)
        if self.stats is not None:
            self.stats.gossip_ok()
        if kind != "push" and result.response is not None:
            followup = self.store.take_repair_response(result.response, source=addr)
            if followup and self._can_send() and not self.leaving:
                self.push(addr, followup)

    def push(self, addr: str, records: list) -> None:
        for msg in self.store.push_messages(records):
            self._send(addr, msg, "push")

    def repair(self, addr: str, kind: str = "repair", on_done=None) -> None:
        self._send(addr, self.store.repair_request(), kind, on_done)

    def on_store_events(self, events: list) -> None:
        """R6.11 and R6.12: event-driven, no scheduler tick involved."""
        if not self._can_send() or self.leaving:
            return
        targets = {}
        for n in self.neighbours():
            records = [ev.record for ev in events if not (ev.kind == FORWARD and ev.source == n)]
            if records:
                targets[n] = records
        for n, records in targets.items():
            self.push(n, records)

    # ------------------------------------------------------------------- timers

    def probe(self) -> Optional[str]:
        """One repair attempt to a random discovered address outside the live
        set, whatever its hint and mark say (R6.10)."""
        outside = self.outside()
        if not outside or not self._can_send():
            return None
        addr = self.rng.choice(outside)
        self.repair(addr, kind="probe")
        return addr

    def repair_tick(self) -> Optional[str]:
        if not self._can_send():
            return None
        addr = self.repair_peer()
        if addr is None:
            return self.probe()                       # every repair tick while the live set is empty
        self.repair(addr)
        return addr

    def tick(self) -> float:
        """Run whatever is due. Returns the next deadline on the monotonic clock."""
        now = self.clock.monotonic()
        if self.leaving:
            return now + 1.0
        if now >= self.next_heartbeat:
            self.store.bump()                         # R6.2: nothing sent
            self.next_heartbeat = self._next(self.next_heartbeat, self.cfg.heartbeat_s, now)
        if now >= self.next_discovery:
            self.next_discovery = self._next(self.next_discovery, self.cfg.discovery_s, now)
            self.refresh_discovery()
            self.probe()                              # every DISCOVERY_SECONDS
        if now >= self.next_repair:
            self.next_repair = self._next(self.next_repair, self.cfg.repair_s, now)
            self.repair_tick()
        else:
            self.neighbours()                         # keep the live set and the counts current
        return min(self.next_heartbeat, self.next_repair, self.next_discovery)

    @staticmethod
    def _next(deadline: float, period: float, now: float) -> float:
        deadline += period
        if deadline <= now:                           # missed ticks are skipped, not replayed
            deadline = now + period
        return deadline

    def on_resume(self, gap: float = 0.0) -> None:
        """R6.22: bump and repair at once."""
        self.store.bump()
        self.next_heartbeat = self.clock.monotonic() + self.cfg.heartbeat_s
        self.repair_tick()

    # --------------------------------------------------------------- lifecycle

    def start(self) -> int:
        """Startup step 5: discovery once, then repairs with up to FANOUT peers
        in parallel. Their follow-up pushes deliver this grange's first record.
        Returns the number of repairs sent; `startup_pending` counts down."""
        self.refresh_discovery()
        targets = self.neighbours()[: self.cfg.fanout]
        with self._lock:
            self.startup_pending = len(targets)

        def one_done(_result):
            with self._lock:
                self.startup_pending -= 1
        for addr in targets:
            if self._can_send():
                self.repair(addr, on_done=one_done)
            else:
                one_done(None)
        now = self.clock.monotonic()
        self.next_repair = now + self.cfg.repair_s
        self.next_discovery = now + self.cfg.discovery_s
        return len(targets)

    def leave(self) -> int:
        """R6.16: a leaving body to all neighbours. Returns the number of pushes;
        the runtime waits at most two seconds for `outstanding` to drain."""
        body = self.store.leave()
        nbrs = self.neighbours()
        with self._lock:
            self.leaving = True
        if not self._can_send():
            return 0
        for n in nbrs:
            for msg in self.store.push_messages([body]):
                self._send(n, msg, "push")
        return len(nbrs)

    def set_stale(self, stale: bool) -> None:
        with self._lock:
            self.stale = stale

    # ----------------------------------------------------------------- logging

    def _log_once(self, key, msg: str) -> None:
        with self._lock:
            if key in self._logged:
                return
            self._logged.add(key)
        self.log(msg)
