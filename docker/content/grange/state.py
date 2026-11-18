"""The Store: everything one grange knows, and the rules for changing it.

Pure Python, no sockets, no threads of its own. Thread-safe: the local API,
the ticker, and the gossip workers all call in. Time comes from the clock
object (monotonic for ages, wall only for the incarnation).

Rules implemented here:
  R4.5   merge, in two parts: content by (incarnation, rev), liveness by
         counter within the stored body's incarnation
  R4.8   incarnation correction, with duplicate-id damping
  R4.9   counter and rev start at 1 and never decrease within an incarnation
  R5.3   farm pushes: what is and is not a content change
  R5.5   the picture: local grange first, then the others by id
  R5.6-8 the cursor and the long-poll wait
  R5.10  repair request and response bodies, stamped with stored liveness (4.2)
  R5.17  the record cap and trimming to the message caps
  R5.18  digest entries refresh only a stored body of the same incarnation
  R6.3   farm_stale, owned by the origin, including the never-pushed case
  R6.4-7 expiry, leaving, the forget map, restart by a greater incarnation

Events: a content change to the own record, an incarnation correction, and
every body accepted with new content are reported to listeners after the
lock is released. The gossip core turns them into pushes (R6.11, R6.12).
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, replace
from typing import Callable, Optional

from .config import (MAX_BODIES_PER_MESSAGE, MAX_GOSSIP_BYTES, MAX_RECORDS,
                     PROTO, Config)
from .model import DigestEntry, GossipMessage, Record, json_size, same_agents

LOCAL = "local"          # event: own record's content changed, or its incarnation was corrected
FORWARD = "forward"      # event: a peer body with new content was accepted

# apply_body outcomes
ACCEPTED = "accepted"    # new content stored (or a leaving body accepted)
REFRESHED = "refreshed"  # liveness only
IGNORED = "ignored"
OWN = "own"              # a version of this grange's own id; see R4.8
OVERFLOW = "overflow"    # a new id beyond the record cap


@dataclass(frozen=True)
class Event:
    kind: str
    record: Record
    source: Optional[str] = None      # address the body came from, for FORWARD


@dataclass
class _Peer:
    body: Record                      # content as accepted; its counter/farm_age_s are not used
    counter: int                      # stored liveness (R4.5)
    farm_age_s: Optional[float]
    farm_age_at: float                # monotonic time farm_age_s was received
    seen_at: float                    # monotonic time of the last liveness refresh


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Store:
    def __init__(self, cfg: Config, clock, log: Callable[[str], None] = _stderr):
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._listeners: list = []

        now = clock.monotonic()
        self.cursor = 1                                  # R5.8: starts again from 1
        self.incarnation = int(clock.wall() * 1000)      # R6.17
        self.counter = 1                                 # R4.9
        self.rev = 1
        self.agents: tuple = ()
        self.farm_stale = False
        self.leaving = False
        self.started_at = now
        self.last_push_at: Optional[float] = None

        self._peers: dict = {}                           # grange id -> _Peer
        self._forget: dict = {}                          # grange id -> ((inc, counter), removed_at)

        self.duplicate_id = False                        # R4.8 damping
        self.duplicate_source: Optional[str] = None
        self.corrections = 0
        self._last_correction_at: Optional[float] = None
        self._last_foreign_at: Optional[float] = None

        self.overflow = 0                                # ids ignored beyond the record cap

    # ------------------------------------------------------------------ plumbing

    @property
    def grange_id(self) -> str:
        return self.cfg.grange_id

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def _dispatch(self, events: list) -> None:
        for ev in events:
            for fn in list(self._listeners):
                fn(ev)

    def _move_cursor(self) -> None:
        self.cursor += 1
        self._changed.notify_all()

    def _now(self) -> float:
        return self.clock.monotonic()

    # ---------------------------------------------------------------- own record

    def _own_farm_age(self, now: float) -> Optional[float]:
        return None if self.last_push_at is None else max(0.0, now - self.last_push_at)

    def _own_body(self, now: float) -> Record:
        return Record(grange=self.grange_id, incarnation=self.incarnation, counter=self.counter,
                      rev=self.rev, farm_age_s=self._own_farm_age(now), farm_stale=self.farm_stale,
                      leaving=self.leaving, agents=self.agents, proto=PROTO, group=self.cfg.group)

    def own_body(self) -> Record:
        with self._lock:
            return self._own_body(self._now())

    def _content_change(self, events: list, now: float) -> None:
        self.rev += 1
        self.counter += 1
        events.append(Event(LOCAL, self._own_body(now)))

    def set_agents(self, agents) -> bool:
        """A farm push (R5.1 to R5.3). Returns True if it was a content change."""
        agents = tuple(agents)
        events: list = []
        with self._lock:
            now = self._now()
            self.last_push_at = now
            if self.leaving:
                return False
            list_changed = not same_agents(agents, self.agents)
            if not list_changed and not self.farm_stale:
                return False                              # liveness is the heartbeat's job
            if list_changed:
                self.agents = agents                      # a reorder alone keeps the stored order
            self.farm_stale = False
            self._content_change(events, now)
            self._move_cursor()
        self._dispatch(events)
        return True

    def bump(self) -> None:
        """Heartbeat (R6.2): counter only; nothing is sent, the cursor stays."""
        with self._lock:
            self.counter += 1

    def evaluate_farm_stale(self) -> bool:
        """R6.3. Returns True if the flag flipped to stale now."""
        events: list = []
        with self._lock:
            now = self._now()
            if self.farm_stale or self.leaving:
                return False
            since = self.last_push_at if self.last_push_at is not None else self.started_at
            if now - since < self.cfg.farm_stale_s:
                return False
            self.farm_stale = True
            self._content_change(events, now)
            self._move_cursor()
        self._dispatch(events)
        return True

    def leave(self) -> Record:
        """The final body sent on SIGTERM (R6.16). Not dispatched as an event:
        the runtime pushes it itself and waits for the pushes."""
        with self._lock:
            now = self._now()
            if not self.leaving:
                self.leaving = True
                self.rev += 1
                self.counter += 1
            return self._own_body(now)

    # ------------------------------------------------------ incarnation correction

    def _seen_own(self, version: tuple, source: Optional[str], events: list, now: float) -> None:
        """A version of our own id arrived from elsewhere (R4.8)."""
        if version <= (self.incarnation, self.counter):
            return                                        # our echo, or lag
        self._last_foreign_at = now
        if self.duplicate_id:
            return                                        # flagged: no further correction
        recent = (self._last_correction_at is not None
                  and now - self._last_correction_at <= self.cfg.forget_s)
        if recent:
            self.duplicate_id = True
            self.duplicate_source = source
            self.log(f"duplicate grange id {self.grange_id!r}: a second greater version arrived "
                     f"within FORGET_SECONDS of a correction, from {source or 'unknown'} "
                     f"(possibly a relay); not correcting again")
            return
        seen_inc = version[0]
        old = self.incarnation
        self.incarnation = max(int(self.clock.wall() * 1000), seen_inc + 1)
        self.counter = 1
        self.rev = 1
        self.corrections += 1
        self._last_correction_at = now
        self.log(f"incarnation corrected from {old} to {self.incarnation}: saw {version} for our own id "
                 f"from {source or 'unknown'}")
        events.append(Event(LOCAL, self._own_body(now)))

    # ------------------------------------------------------------------- merging

    def apply_body(self, record: Record, source: Optional[str] = None) -> str:
        events: list = []
        with self._lock:
            outcome = self._apply_body(record, source, events, self._now())
        self._dispatch(events)
        return outcome

    def _apply_body(self, rec: Record, source, events: list, now: float) -> str:
        gid = rec.grange
        if gid == self.grange_id:
            self._seen_own(rec.liveness_version(), source, events, now)
            return OWN

        peer = self._peers.get(gid)
        if peer is None:
            forgotten = self._forget.get(gid)
            if forgotten is not None:
                remembered, removed_at = forgotten
                if now - removed_at <= self.cfg.forget_s and rec.liveness_version() <= remembered:
                    return IGNORED                        # R6.6: a lagging copy cannot resurrect it
                del self._forget[gid]
            if rec.leaving:
                # Accepted under R4.5 (no body stored) and removed at once (R6.5).
                self._forget[gid] = (rec.liveness_version(), now)
                events.append(Event(FORWARD, self._stamp(rec, rec.counter, rec.farm_age_s, 0.0), source))
                return ACCEPTED
            if len(self._peers) + 1 >= MAX_RECORDS:
                self.overflow += 1
                return OVERFLOW
            self._peers[gid] = _Peer(body=rec, counter=rec.counter, farm_age_s=rec.farm_age_s,
                                     farm_age_at=now, seen_at=now)
            self._move_cursor()                           # first body, or readmitted
            events.append(Event(FORWARD, self._stamp_peer(gid, now), source))
            return ACCEPTED

        outcome = IGNORED
        stored_inc = peer.body.incarnation
        if rec.content_version() > peer.body.content_version():
            peer.body = rec
            if rec.incarnation > stored_inc:              # a new life: liveness taken whole
                peer.counter, peer.farm_age_s = rec.counter, rec.farm_age_s
                peer.farm_age_at = peer.seen_at = now
            outcome = ACCEPTED
        if rec.incarnation == peer.body.incarnation and rec.counter > peer.counter:
            peer.counter, peer.farm_age_s = rec.counter, rec.farm_age_s
            peer.farm_age_at = peer.seen_at = now
            outcome = outcome if outcome == ACCEPTED else REFRESHED
        if outcome != ACCEPTED:
            return outcome

        stamped = self._stamp_peer(gid, now)
        if rec.leaving:                                   # R6.5
            del self._peers[gid]
            self._forget[gid] = ((peer.body.incarnation, peer.counter), now)
        self._move_cursor()
        events.append(Event(FORWARD, stamped, source))
        return ACCEPTED

    def apply_digest(self, digest: dict, source: Optional[str] = None) -> int:
        """Liveness half of R4.5 and R5.18. Returns the number of peers refreshed."""
        events: list = []
        with self._lock:
            refreshed = self._apply_digest(digest, source, events, self._now())
        self._dispatch(events)
        return refreshed

    def _apply_digest(self, digest: dict, source, events: list, now: float) -> int:
        refreshed = 0
        for gid, entry in digest.items():
            if gid == self.grange_id:
                self._seen_own(entry.liveness_version(), source, events, now)
                continue
            peer = self._peers.get(gid)
            if peer is None or entry.incarnation != peer.body.incarnation:
                continue                                  # creates, readmits, and refreshes nothing
            if entry.counter > peer.counter:
                peer.counter, peer.farm_age_s = entry.counter, entry.farm_age_s
                peer.farm_age_at = peer.seen_at = now
                refreshed += 1
        return refreshed

    # -------------------------------------------------------------------- expiry

    def expire(self) -> list:
        """R6.4: remove peers whose seen age exceeds EXPIRE_SECONDS. Also ages out
        the forget map and the duplicate-id flag. Returns the ids removed."""
        with self._lock:
            now = self._now()
            gone = [gid for gid, p in self._peers.items() if now - p.seen_at > self.cfg.expire_s]
            for gid in gone:
                peer = self._peers.pop(gid)
                self._forget[gid] = ((peer.body.incarnation, peer.counter), now)
                self._move_cursor()
            for gid in [g for g, (_, at) in self._forget.items() if now - at > self.cfg.forget_s]:
                del self._forget[gid]
            if (self.duplicate_id and self._last_foreign_at is not None
                    and now - self._last_foreign_at > self.cfg.forget_s):
                self.duplicate_id = False
                self.duplicate_source = None
                self.log("duplicate grange id flag cleared: no foreign greater version for FORGET_SECONDS")
            return gone

    # ----------------------------------------------------------------- outgoing

    def _stamp(self, rec: Record, counter: int, farm_age_s, held_for: float) -> Record:
        age = None if farm_age_s is None else farm_age_s + max(0.0, held_for)
        return replace(rec, counter=counter, farm_age_s=age, group=self.cfg.group, proto=PROTO)

    def _stamp_peer(self, gid: str, now: float) -> Record:
        p = self._peers[gid]
        return self._stamp(p.body, p.counter, p.farm_age_s, now - p.farm_age_at)

    def _entries(self, now: float) -> dict:
        """grange id -> (stamped body) for everything held, own record first."""
        out = {self.grange_id: self._own_body(now)}
        for gid in sorted(self._peers):
            out[gid] = self._stamp_peer(gid, now)
        return out

    def digest(self) -> dict:
        with self._lock:
            return self._digest(self._now())

    def _digest(self, now: float) -> dict:
        return {gid: DigestEntry(r.incarnation, r.counter, r.rev, r.farm_age_s)
                for gid, r in self._entries(now).items()}

    def body(self, gid: str) -> Optional[Record]:
        """The stamped body held for an id, own included, or None."""
        with self._lock:
            now = self._now()
            if gid == self.grange_id:
                return self._own_body(now)
            return self._stamp_peer(gid, now) if gid in self._peers else None

    def bodies_newer_than(self, digest: dict) -> list:
        """Bodies whose (incarnation, rev) is greater than the digest's entry."""
        with self._lock:
            return [r for gid, r in self._entries(self._now()).items()
                    if gid in digest and r.content_version() > digest[gid].content_version()]

    def bodies_missing_from(self, digest: dict) -> list:
        with self._lock:
            return [r for gid, r in self._entries(self._now()).items() if gid not in digest]

    def _bodies_for(self, digest: dict, now: float) -> list:
        """Newer or missing bodies, own record first (R5.10)."""
        return [r for gid, r in self._entries(now).items()
                if gid not in digest or r.content_version() > digest[gid].content_version()]

    # ------------------------------------------------------------ gossip messages

    def _message(self, digest: Optional[dict], records: list) -> GossipMessage:
        return GossipMessage(proto=PROTO, group=self.cfg.group, sender=self.grange_id,
                             digest=digest, records=records)

    def trim(self, digest: Optional[dict], records: list) -> GossipMessage:
        """As many bodies as fit the caps go now; the rest follow later (R5.17)."""
        msg = self._message(digest, [])
        budget = MAX_GOSSIP_BYTES - json_size(msg.to_json()) - 64
        kept = []
        for rec in records:
            if len(kept) >= MAX_BODIES_PER_MESSAGE:
                break
            size = json_size(rec.to_json()) + 1
            if size > budget:
                continue
            kept.append(rec)
            budget -= size
        msg.records = kept
        return msg

    def push_message(self, records: list) -> GossipMessage:
        return self.trim(None, list(records))

    def repair_request(self) -> GossipMessage:
        with self._lock:
            return self._message(self._digest(self._now()), [])

    def answer_repair(self, msg: GossipMessage, source: Optional[str] = None) -> GossipMessage:
        """Receiver side of a repair (R5.10): merge the digest, answer with our
        digest plus every body the requester lacks or holds older."""
        events: list = []
        with self._lock:
            now = self._now()
            self._apply_digest(msg.digest or {}, source, events, now)
            reply = self.trim(self._digest(now), self._bodies_for(msg.digest or {}, now))
        self._dispatch(events)
        return reply

    def take_repair_response(self, msg: GossipMessage, source: Optional[str] = None) -> list:
        """Requester side: merge the response, then return the follow-up bodies
        to push back, those greater than or missing from its digest."""
        events: list = []
        with self._lock:
            now = self._now()
            for rec in msg.records:
                self._apply_body(rec, source, events, now)
            self._apply_digest(msg.digest or {}, source, events, now)
            followup = self._bodies_for(msg.digest or {}, now)
        self._dispatch(events)
        return followup

    def take_push(self, msg: GossipMessage, source: Optional[str] = None) -> list:
        """Receiver side of a push. Returns the outcomes, in order."""
        events: list = []
        with self._lock:
            now = self._now()
            outcomes = [self._apply_body(rec, source, events, now) for rec in msg.records]
        self._dispatch(events)
        return outcomes

    # ----------------------------------------------------------------- reading

    def is_live_record(self, gid: str) -> bool:
        with self._lock:
            return gid in self._peers

    def peer_ids(self) -> list:
        with self._lock:
            return sorted(self._peers)

    def remembered(self, gid: str):
        """The forget map's (incarnation, counter) for an id, or None."""
        with self._lock:
            entry = self._forget.get(gid)
            return None if entry is None else entry[0]

    def seen_age(self, gid: str) -> Optional[float]:
        with self._lock:
            p = self._peers.get(gid)
            return None if p is None else self._now() - p.seen_at

    def picture(self) -> dict:
        with self._lock:
            return self._picture(self._now())

    def _picture(self, now: float) -> dict:
        own_age = self._own_farm_age(now)
        granges = [{
            "grange": self.grange_id,
            "seen_age_s": 0.0,
            "farm_age_s": None if own_age is None else round(own_age, 1),
            "farm_stale": self.farm_stale,
            "agents": [a.to_json() for a in self.agents],
        }]
        for gid in sorted(self._peers):
            p = self._peers[gid]
            age = None if p.farm_age_s is None else p.farm_age_s + (now - p.farm_age_at)
            granges.append({
                "grange": gid,
                "seen_age_s": round(now - p.seen_at, 1),
                "farm_age_s": None if age is None else round(age, 1),
                "farm_stale": p.body.farm_stale,
                "agents": [a.to_json() for a in p.body.agents],
            })
        return {"cursor": self.cursor, "grange": self.grange_id,
                "incarnation": self.incarnation, "granges": granges}

    def wait_for(self, after: Optional[int], timeout: float) -> dict:
        """R5.7, R5.8: only `after` equal to the cursor waits, up to `timeout`."""
        with self._lock:
            if after is not None and after == self.cursor and timeout > 0:
                self._changed.wait_for(lambda: self.cursor != after, timeout)
            return self._picture(self._now())

    def health(self) -> dict:
        with self._lock:
            return {"cursor": self.cursor, "incarnation": self.incarnation,
                    "duplicate_id": self.duplicate_id, "records": len(self._peers) + 1,
                    "overflow_ids": self.overflow}
