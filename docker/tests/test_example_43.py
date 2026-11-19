"""Replay of the worked example in requirements section 4.3.

Three granges, Don (djp3), Alice, and Bob, each a Store on its own fake clock,
exchanging bodies and digests by direct calls. Times are Don's clock in the
text; here they are seconds since 09:00:00. The replay asserts the versions
the text gives and, at each step, whether each cursor moved (not by how
much, R5.6). A number this replay contradicts is to be corrected in the
requirements, not papered over here.

The ticker and the gossip runtime arrive in later phases; `Mesh` stands in
for them with the rules they implement: heartbeat every HEARTBEAT_SECONDS,
one repair every REPAIR_SECONDS (on a fixed rota, for determinism), event-
driven pushes to every other grange with forward-once, and a once-a-second
ticker run of expiry, the farm_stale check, and resume detection (R6.22).
"""
from __future__ import annotations

import unittest

from grange.clock import FakeClock
from grange.config import RESUME_GAP_SECONDS
from grange.model import DigestEntry, Record
from grange.state import FORWARD, IGNORED, LOCAL

from helpers import agent, make_store

T0 = 1_763_974_800.0          # 09:00:00 on Don's clock
I1 = 1_763_974_800_000


def at(hms: str) -> int:
    """'09:01:12' -> seconds since 09:00:00."""
    h, m, s = (int(x) for x in hms.split(":"))
    return (h - 9) * 3600 + m * 60 + s


class Node:
    def __init__(self, mesh, addr, gid, t):
        self.addr = addr
        self.clock = FakeClock(monotonic=10_000.0 + t, wall=T0 + t)
        self.store = make_store(gid, self.clock)
        self.store.subscribe(lambda ev: mesh.pending.append((addr, ev)))
        self.up = True
        self.frozen = False
        self.next_beat = self.clock.monotonic() + self.store.cfg.heartbeat_s
        self.last_tick = self.clock.monotonic()
        self.farm = None              # agent list farm reports, or None while farm is down
        self.resumes = 0


class Mesh:
    """Three granges, all neighbours of each other."""

    ROTA = {0: [("alice", "don")], 1: [("bob", "don")], 2: [("alice", "bob"), ("don", "alice")]}

    def __init__(self, start: int):
        self.t = start
        self.nodes: dict = {}
        self.pending: list = []
        self.sent: list = []          # (t, from, to, grange, incarnation, rev, counter)
        self.script: dict = {}

    # --------------------------------------------------------------- plumbing
    def start(self, addr: str, gid: str) -> Node:
        node = Node(self, addr, gid, self.t)
        self.nodes[addr] = node
        return node

    def store(self, addr):
        return self.nodes[addr].store

    def cursors(self) -> dict:
        return {a: n.store.cursor for a, n in self.nodes.items()}

    def _deliver(self, frm: str, to: str, records: list) -> None:
        target = self.nodes.get(to)
        if not records or target is None or not target.up or not self.nodes[frm].up:
            return
        for r in records:
            self.sent.append((self.t, frm, to, r.grange, r.incarnation, r.rev, r.counter))
        target.store.take_push(self.store(frm).push_message(records), source=frm)

    def flush(self) -> None:
        """Event-driven pushes (R6.11) and forward-once (R6.12)."""
        while self.pending:
            frm, ev = self.pending.pop(0)
            for to in self.nodes:
                if to != frm and to != ev.source:
                    self._deliver(frm, to, [ev.record])

    def repair(self, requester: str, responder: str):
        a, b = self.nodes[requester], self.nodes[responder]
        if not (a.up and b.up):
            return None
        response = b.store.answer_repair(a.store.repair_request(), source=requester)
        for r in response.records:
            self.sent.append((self.t, responder, requester, r.grange, r.incarnation, r.rev, r.counter))
        followup = a.store.take_repair_response(response, source=responder)
        self._deliver(requester, responder, followup)
        self.flush()
        return response

    def startup_repairs(self, requester: str, peers) -> dict:
        """6.1 step 5: repairs with several peers in parallel. Every request
        is answered before any response is merged or anything is forwarded."""
        a = self.nodes[requester]
        request = a.store.repair_request()
        responses = {p: self.store(p).answer_repair(request, source=requester)
                     for p in peers if self.nodes[p].up}
        for peer, response in responses.items():
            followup = a.store.take_repair_response(response, source=peer)
            self._deliver(requester, peer, followup)
        self.flush()
        return responses

    def farm_push(self, addr: str, agents=None) -> bool:
        node = self.nodes[addr]
        if agents is not None:
            node.farm = list(agents)
        changed = node.store.set_agents(node.farm)
        self.flush()
        return changed

    def tick(self, addr: str) -> None:
        """What the ticker does once a second: expiry, farm_stale, resume."""
        node = self.nodes[addr]
        now = node.clock.monotonic()
        gap = now - node.last_tick
        node.last_tick = now
        node.store.expire()
        if gap > RESUME_GAP_SECONDS:                      # R6.22
            node.resumes += 1
            node.store.bump()
            peer = next(a for a in self.nodes if a != addr and self.nodes[a].up)
            self.repair(addr, peer)
        node.store.evaluate_farm_stale()
        self.flush()

    def at(self, t: int, fn) -> None:
        self.script.setdefault(t, []).append(fn)

    # ------------------------------------------------------------------ time
    def run_to(self, t_end: int) -> None:
        while self.t < t_end:
            self.t += 1
            for node in self.nodes.values():
                if not node.frozen:
                    node.clock.advance(1)
            for fn in self.script.pop(self.t, []):
                fn()
            for requester, responder in self.ROTA[self.t % 3]:
                if requester in self.nodes and responder in self.nodes:
                    self.repair(requester, responder)
            for node in self.nodes.values():
                if node.up and node.clock.monotonic() >= node.next_beat:
                    node.store.bump()                             # R6.2: nothing sent
                    while node.next_beat <= node.clock.monotonic():
                        node.next_beat += node.store.cfg.heartbeat_s
            for addr, node in self.nodes.items():
                if node.up and node.farm is not None and self.t % 30 == 0:
                    node.store.set_agents(node.farm)              # farm's heartbeat push (R5.2)
            for addr, node in list(self.nodes.items()):
                if node.up:
                    self.tick(addr)
            self.flush()


def two_sessions(state2="busy"):
    return [agent("proj-86-pr-98 fix tests", "busy"), agent("proj-91-pr-102 review", state2)]


def held(store, gid):
    rec = store.body(gid)
    return None if rec is None else (rec.incarnation, rec.counter, rec.rev)


class WorkedExampleTest(unittest.TestCase):
    maxDiff = None

    def build_to_lid_close(self):
        """Steps 1 to 7 and the run-up to step 8. Returns the mesh at 12:30:01."""
        m = Mesh(start=-60)
        alice = m.start("alice", "alice")
        bob = m.start("bob", "bob")
        alice.farm = [agent("proj-12-pr-7 docs", "idle", owner="alice")]
        bob.farm = [agent("proj-40-pr-3 api", "busy", owner="bob")]
        m.farm_push("alice"), m.farm_push("bob")
        m.repair("alice", "bob")
        m.run_to(0)

        # ---- 1. Start ------------------------------------------------------
        before = m.cursors()
        don = m.start("don", "djp3")
        d = don.store
        self.assertEqual(d.incarnation, I1)
        self.assertEqual((d.counter, d.rev, d.agents, d.own_body().farm_age_s), (1, 1, (), None))
        for peer, response in m.startup_repairs("don", ["alice", "bob"]).items():
            self.assertNotIn("djp3", response.digest)          # so the follow-up hands his record over
            self.assertEqual(held(m.store(peer), "djp3"), (I1, 1, 1))
        after = m.cursors()
        self.assertGreater(after["alice"], before["alice"])      # Don first appears
        self.assertGreater(after["bob"], before["bob"])

        # ---- 2. First farm push at 09:00:05 ---------------------------------
        m.run_to(at("09:00:04"))
        before = m.cursors()
        m.at(at("09:00:05"), lambda: self.assertTrue(m.farm_push("don", two_sessions())))
        m.run_to(at("09:00:05"))
        self.assertEqual((d.rev, d.counter), (2, 2))
        self.assertEqual(len(m.store("alice").picture()["granges"][2]["agents"]), 2)   # alice, bob, djp3
        self.assertEqual(held(m.store("alice"), "djp3")[2], 2)
        self.assertEqual(held(m.store("bob"), "djp3")[2], 2)
        self.assertGreater(m.cursors()["alice"], before["alice"])
        self.assertEqual(m.pending, [])                          # forward-once settled, no loop

        # ---- 3. Heartbeats, 09:00:05 to 09:01:00 ----------------------------
        before, sent_before = m.cursors(), len(m.sent)
        seen, last = [], held(m.store("alice"), "djp3")[1]
        for t in range(at("09:00:06"), at("09:01:00") + 1):
            m.run_to(t)
            now = held(m.store("alice"), "djp3")[1]
            seen.append(now)
            if now != last:                                      # each climb resets her seen age
                self.assertEqual(m.store("alice").seen_age("djp3"), 0.0)
            last = now
        self.assertEqual(seen[0], 2)
        self.assertEqual(sorted(set(seen))[1:4], [3, 4, 5])      # "climbs, 3, 4, 5"
        self.assertEqual(d.counter, 8)                           # by 09:01:00
        self.assertEqual(d.rev, 2)
        self.assertEqual([s for s in m.sent[sent_before:] if s[3] == "djp3"], [])   # never resent
        self.assertEqual(m.cursors(), before)

        # ---- 4. A state change at 09:01:12; farm's heartbeat at 09:01:30 ---
        m.run_to(at("09:01:10"))
        self.assertEqual(d.counter, 9)
        m.at(at("09:01:11"), lambda: (m.repair("bob", "don"), setattr(m.nodes["bob"], "up", False)))
        before = m.cursors()
        m.at(at("09:01:12"), lambda: self.assertTrue(m.farm_push("don", two_sessions("waiting"))))
        m.run_to(at("09:01:12"))
        self.assertEqual((d.rev, d.counter), (3, 10))
        self.assertEqual(held(m.store("alice"), "djp3")[2], 3)
        self.assertGreater(m.cursors()["alice"], before["alice"])
        m.run_to(at("09:01:29"))
        rev, counter = d.rev, d.counter
        sent_before = len(m.sent)
        m.at(at("09:01:30"), lambda: self.assertFalse(m.farm_push("don")))
        m.run_to(at("09:01:30"))
        self.assertEqual(d.rev, rev)
        self.assertEqual(d.counter, counter + 1)                 # the 09:01:30 heartbeat, not the push
        self.assertEqual(d.own_body().farm_age_s, 0.0)
        self.assertEqual([s for s in m.sent[sent_before:] if s[3] == "djp3"], [])

        # ---- 5. A copy that is behind: Bob repairs with Alice at 09:02:00 --
        m.run_to(at("09:01:59"))
        self.assertEqual(held(m.store("bob"), "djp3"), (I1, 9, 2))
        self.assertEqual(held(m.store("alice"), "djp3"), (I1, 14, 3))
        before = m.cursors()

        def bob_returns():
            m.nodes["bob"].up = True
            response = m.repair("bob", "alice")
            relayed = {r.grange: r for r in response.records}["djp3"]
            self.assertEqual((relayed.rev, relayed.counter), (3, 14))
            self.assertEqual(held(m.store("alice"), "djp3"), (I1, 14, 3))   # refreshed nothing
            self.assertEqual(held(m.store("bob"), "djp3"), (I1, 14, 3))
            self.assertGreater(m.store("bob").cursor, before["bob"])
            self.assertEqual(m.store("alice").cursor, before["alice"])
        m.at(at("09:02:00"), bob_returns)
        m.run_to(at("09:02:00"))
        slow = Record(grange="djp3", incarnation=I1, counter=7, rev=2, farm_age_s=0.0,
                      farm_stale=False, leaving=False, agents=tuple(two_sessions()))
        self.assertEqual(m.store("alice").apply_body(slow, source="slow-neighbour"), IGNORED)
        self.assertEqual(held(m.store("alice"), "djp3")[2], 3)

        # ---- 6. Farm goes quiet at 09:05:00, comes back at 09:08:00 --------
        m.run_to(at("09:05:00"))
        don.farm = None                                          # farm crashes after its 09:05:00 push
        m.run_to(at("09:05:59"))
        self.assertFalse(d.farm_stale)
        before = m.cursors()
        m.run_to(at("09:06:00"))
        self.assertTrue(d.farm_stale)
        self.assertEqual(d.rev, 4)
        alice_view = {g["grange"]: g for g in m.store("alice").picture()["granges"]}["djp3"]
        self.assertTrue(alice_view["farm_stale"])
        self.assertEqual(len(alice_view["agents"]), 2)           # flagged, not removed
        self.assertGreater(m.cursors()["alice"], before["alice"])
        before = m.cursors()
        m.at(at("09:08:00"), lambda: self.assertTrue(m.farm_push("don", two_sessions("waiting"))))
        m.run_to(at("09:08:00"))
        self.assertEqual(d.rev, 5)
        self.assertFalse(d.farm_stale)
        self.assertGreater(m.cursors()["alice"], before["alice"])

        # ---- 7. Update and restart at 09:10:03 -----------------------------
        m.run_to(at("09:10:02"))
        before = m.cursors()
        final = d.leave()
        self.assertEqual((final.leaving, final.rev, final.counter), (True, 6, 66))
        for peer in ("alice", "bob"):
            m._deliver("don", peer, [final])
        m.flush()
        m.nodes["don"].up = False
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).body("djp3"))
            self.assertEqual(m.store(peer).remembered("djp3"), (I1, 66))
        self.assertGreater(m.cursors()["alice"], before["alice"])
        old_body = Record(grange="djp3", incarnation=I1, counter=65, rev=5, farm_age_s=1.0,
                          farm_stale=False, leaving=False, agents=tuple(two_sessions("waiting")))

        m.run_to(at("09:10:19"))
        m.t += 1                                                 # 09:10:20: the new container
        for node in m.nodes.values():
            node.clock.advance(1)
        del m.nodes["don"]
        before = m.cursors()
        don = m.start("don", "djp3")
        d = don.store
        I2 = d.incarnation
        self.assertGreater(I2, I1)
        self.assertEqual((d.counter, d.rev), (1, 1))
        m.startup_repairs("don", ["alice", "bob"])               # 6.1 step 5
        # The startup repair's follow-up push hands Alice "I2, counter 1,
        # rev 1" with an empty agent list: counter 1 is far below the 66 she
        # remembers, but I2 beats I1, so the new life is accepted.
        self.assertEqual(held(m.store("alice"), "djp3"), (I2, 1, 1))
        self.assertEqual(m.store("alice").body("djp3").agents, ())
        m.at(at("09:10:21"), lambda: m.farm_push("don", two_sessions("waiting")))
        m.run_to(at("09:10:21"))
        self.assertEqual(held(m.store("alice"), "djp3"), (I2, 2, 2))   # farm's first push
        self.assertEqual(len(m.store("alice").body("djp3").agents), 2)  # his agents are back
        self.assertGreater(m.store("alice").cursor, before["alice"])
        self.assertEqual(m.store("alice").apply_body(old_body, source="bob"), IGNORED)  # lagging I1 copy

        # ---- run-up to step 8: three hours of normal work ------------------
        states = ["busy", "waiting"]
        for k in range(100):                                     # 100 content changes, one every 2 min
            t = at("09:11:00") + 120 * k
            m.at(t, lambda k=k: self.assertTrue(m.farm_push("don", two_sessions(states[k % 2]))))
        m.run_to(at("12:30:00"))
        self.assertEqual(d.counter, 1300)
        m.at(at("12:30:01"), lambda: (m.repair("alice", "don"), m.repair("bob", "don")))
        m.run_to(at("12:30:01"))
        for peer in ("alice", "bob"):
            self.assertEqual(held(m.store(peer), "djp3"), (I2, 1300, 102))
        return m, I2

    # ------------------------------------------------------------ steps 1 to 8a
    def test_steps_1_to_8_stopwatch_runs_on(self):
        m, I2 = self.build_to_lid_close()
        don, d = m.nodes["don"], m.store("don")
        don.up = False
        don.farm = None                                           # the whole machine sleeps
        before = m.cursors()
        m.run_to(at("12:31:05"))                                 # "by 12:31": last refresh was 12:30:01
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).body("djp3"))         # dropped by 12:31
            self.assertEqual(m.store(peer).remembered("djp3"), (I2, 1300))
            self.assertGreater(m.cursors()[peer], before[peer])
        m.run_to(at("12:41:00"))
        self.assertEqual(m.store("alice").remembered("djp3"), (I2, 1300))
        m.run_to(at("12:41:10"))
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).remembered("djp3"))   # forgotten "until about 12:41"
        m.run_to(at("13:14:59"))

        # 13:15:00, the lid opens. The stopwatch ran on, as under docker pause.
        def wake():
            don.up = True
            c0 = d.cursor
            expired = d.expire()
            self.assertEqual(sorted(expired), ["alice", "bob"])   # the wake first expires everyone
            c1 = d.cursor
            self.assertGreater(c1, c0)
            m.tick("don")                                         # sees the 45-minute gap
            self.assertEqual(don.resumes, 1)
            self.assertGreater(d.cursor, c1)                      # the repair response refilled it
            self.assertEqual(sorted(g["grange"] for g in d.picture()["granges"]), ["alice", "bob", "djp3"])
            for peer in ("alice", "bob"):
                self.assertIsNotNone(m.store(peer).body("djp3"))  # back on every board
                self.assertGreater(m.store(peer).body("djp3").counter, 1300)
            self.assertTrue(d.farm_stale)                         # farm looks 45 minutes silent
        m.at(at("13:15:00"), wake)
        m.run_to(at("13:15:00"))
        self.assertTrue({g["grange"]: g for g in m.store("alice").picture()["granges"]}["djp3"]["farm_stale"])
        m.at(at("13:15:05"), lambda: self.assertTrue(m.farm_push("don", two_sessions())))
        m.run_to(at("13:15:05"))
        self.assertFalse({g["grange"]: g for g in m.store("alice").picture()["granges"]}["djp3"]["farm_stale"])

    # -------------------------------------------------------------- step 8b
    def sleep_with_stopwatch_still(self, wake_at: str):
        """Lid closed at 12:30:01 with Don's monotonic clock frozen until `wake_at`.
        Returns (mesh, I2, Don's cursor and board before the wake)."""
        m, I2 = self.build_to_lid_close()
        don, d = m.nodes["don"], m.store("don")
        don.up, don.frozen = False, True
        farm, don.farm = don.farm, None
        m.run_to(at("12:31:05"))                                 # "by 12:31": last refresh was 12:30:01
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).body("djp3"))
            self.assertEqual(m.store(peer).remembered("djp3"), (I2, 1300))
        m.run_to(at(wake_at) - 1)
        before = (d.cursor, [g["grange"] for g in d.picture()["granges"]])

        def wake():
            don.up, don.frozen, don.farm = True, False, farm
        m.at(at(wake_at), wake)
        return m, I2, before

    def back_on_every_board_within(self, m, wake_at: str, limit: int) -> int:
        for t in range(at(wake_at), at(wake_at) + limit + 1):
            m.run_to(t)
            if all(m.store(p).body("djp3") is not None for p in ("alice", "bob")):
                return t - at(wake_at)
        self.fail(f"Don not back on every board within {limit} s of waking")

    def test_step_8_stopwatch_stood_still_45_minutes(self):
        """The text's 45-minute sleep with the stopwatch frozen. By 13:15 the
        peers forgot (I2, 1300) long ago, so the next repair, at most
        REPAIR_SECONDS away, readmits Don at counter 1300 with no heartbeat."""
        m, I2, (don_cursor, board_before) = self.sleep_with_stopwatch_still("13:15:00")
        don, d = m.nodes["don"], m.store("don")
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).remembered("djp3"))  # no peer remembers his version
        back = self.back_on_every_board_within(m, "13:15:00", 20)
        self.assertLessEqual(back, d.cfg.repair_s)                # "at most 3 seconds away"
        self.assertEqual(don.resumes, 0)                          # no gap is seen
        self.assertEqual(m.store("alice").body("djp3").counter, 1300)   # accepted whatever its counter
        self.assertFalse(d.farm_stale)
        self.assertEqual(d.cursor, don_cursor)
        self.assertEqual([g["grange"] for g in d.picture()["granges"]], board_before)

    # ---------------------------------------------- step 8, sleep under ten minutes
    def test_step_8_short_sleep_stopwatch_ran_on(self):
        """Waking at 12:35, when the peers still remember (I2, 1300): the
        R6.22 bump lifts the counter above it, so the immediate repair
        readmits Don."""
        m, I2 = self.build_to_lid_close()
        don, d = m.nodes["don"], m.store("don")
        don.up, don.farm = False, None
        m.run_to(at("12:34:59"))
        for peer in ("alice", "bob"):
            self.assertIsNone(m.store(peer).body("djp3"))
            self.assertEqual(m.store(peer).remembered("djp3"), (I2, 1300))

        def wake():
            don.up = True
            m.tick("don")                                         # sees the gap
            self.assertEqual(don.resumes, 1)
            first = next(s for s in m.sent if s[0] == m.t and s[3] == "djp3")
            self.assertEqual(first[6], 1301)                      # the follow-up push, after the bump
            for peer in ("alice", "bob"):
                self.assertEqual(held(m.store(peer), "djp3")[0], I2)          # readmitted at once
                self.assertGreater(held(m.store(peer), "djp3")[1], 1300)
            self.assertTrue(d.farm_stale)                         # farm looks five minutes silent
        m.at(at("12:35:00"), wake)
        m.run_to(at("12:35:00"))

    def test_step_8_short_sleep_stopwatch_stood_still(self):
        """Waking at 12:35 with the stopwatch frozen: counter 1300 does not
        beat the remembered 1300 and is refused; the next heartbeat makes it
        1301 and the next repair delivers that, within about 13 seconds."""
        m, I2, (don_cursor, board_before) = self.sleep_with_stopwatch_still("12:35:00")
        don, d = m.nodes["don"], m.store("don")
        m.run_to(at("12:35:00"))
        self.assertEqual(don.resumes, 0)                          # no gap is seen
        self.assertEqual(m.store("alice").remembered("djp3"), (I2, 1300))
        self.assertIsNone(m.store("alice").body("djp3"))          # counter 1300 offered and refused
        self.assertEqual(d.counter, 1300)
        back = self.back_on_every_board_within(m, "12:35:00", 20)
        self.assertLessEqual(back, 13)                            # "within about 13 seconds"
        self.assertEqual(m.store("alice").body("djp3").counter, 1301)
        self.assertFalse(d.farm_stale)                            # farm does not look silent
        self.assertEqual(d.cursor, don_cursor)                    # nothing really changed
        self.assertEqual([g["grange"] for g in d.picture()["granges"]], board_before)


class ClockWasWrongTest(unittest.TestCase):
    """Step 9: a restart after a backward clock step heals in one exchange."""

    def setUp(self):
        self.m = Mesh(start=0)
        self.m.start("alice", "alice"), self.m.start("bob", "bob")
        self.m.repair("alice", "bob")

    def start_ahead(self):
        """Don's grange starts while his clock is an hour ahead: incarnation I3."""
        m = self.m
        node = Node(m, "don", "djp3", m.t)
        node.clock.set_wall(T0 + m.t + 3600)
        node.store = make_store("djp3", node.clock)
        node.store.subscribe(lambda ev: m.pending.append(("don", ev)))
        m.nodes["don"] = node
        for peer in ("alice", "bob"):
            m.repair("don", peer)
        m.at(m.t + 1, lambda: m.farm_push("don", two_sessions()))
        return node

    def test_crash_then_restart_heals_in_one_exchange(self):
        m = self.m
        node = self.start_ahead()
        I3 = node.store.incarnation
        m.run_to(m.t + 30)
        m.repair("alice", "don"), m.repair("bob", "don")
        for peer in ("alice", "bob"):
            self.assertEqual(held(m.store(peer), "djp3")[:2], (I3, 5))
        del m.nodes["don"]                                        # crash: no leaving body
        restarted = m.start("don", "djp3")                       # NTP fixed the clock
        I4 = restarted.store.incarnation
        self.assertLess(I4, I3)
        # Had a copy of the I4 body reached a peer first, it would have been ignored.
        self.assertEqual(m.store("bob").apply_body(restarted.store.own_body()), IGNORED)
        before = m.cursors()
        m.repair("don", "alice")                                  # the startup repair
        d = restarted.store
        self.assertEqual((d.incarnation, d.counter, d.rev), (I3 + 1, 1, 1))
        self.assertEqual(d.corrections, 1)
        for peer in ("alice", "bob"):
            self.assertEqual(held(m.store(peer), "djp3"), (I3 + 1, 1, 1))   # everyone accepts
        self.assertGreater(m.cursors()["alice"], before["alice"])

    def test_after_a_clean_stop_the_new_record_waits_out_the_forget_map(self):
        m = self.m
        node = self.start_ahead()
        I3 = node.store.incarnation
        m.run_to(m.t + 30)
        final = node.store.leave()
        for peer in ("alice", "bob"):
            m._deliver("don", peer, [final])
        removed_at = m.t
        del m.nodes["don"]
        restarted = m.start("don", "djp3")
        for peer in ("alice", "bob"):
            response = m.repair("don", peer)
            self.assertNotIn("djp3", response.digest)            # nothing lists I3
        self.assertEqual(restarted.store.corrections, 0)
        self.assertIsNone(m.store("alice").body("djp3"))          # refused silently
        self.assertLess(restarted.store.incarnation, I3)
        m.run_to(removed_at + 600)
        self.assertIsNone(m.store("alice").body("djp3"))          # still within ten minutes
        m.run_to(removed_at + 603)
        self.assertIsNotNone(m.store("alice").body("djp3"))       # then accepted as a fresh start
        self.assertEqual(m.store("alice").body("djp3").incarnation, restarted.store.incarnation)


class TwoGrangesOneIdTest(unittest.TestCase):
    """Step 10: Bob copies Don's .env and starts a second djp3."""

    def test_settles_with_one_flagged(self):
        m = Mesh(start=0)
        m.start("alice", "alice")
        m.start("don", "djp3")
        m.repair("don", "alice")
        m.run_to(30)
        before = m.cursors()
        bob_dup = m.start("bob", "djp3")                          # I5, greater than Don's
        I5 = bob_dup.store.incarnation
        self.assertGreater(I5, m.store("don").incarnation)
        m.run_to(60)
        don, dup = m.store("don"), bob_dup.store
        self.assertEqual(don.corrections, 1)
        self.assertEqual(dup.corrections, 1)
        self.assertTrue(don.duplicate_id)                         # second trigger within ten minutes
        self.assertFalse(dup.duplicate_id)                        # never sees a greater version again
        self.assertTrue(any("duplicate grange id" in line for line in don.log))
        self.assertEqual(held(m.store("alice"), "djp3")[0], dup.incarnation)   # the mesh shows Bob's
        self.assertGreater(m.cursors()["alice"], before["alice"])
        m.run_to(400)
        self.assertEqual((don.corrections, dup.corrections), (1, 1))         # bounded
        self.assertEqual(don.picture()["granges"][0]["grange"], "djp3")      # Don's own board works
        self.assertTrue(don.health()["duplicate_id"])

        # Bob fixes his .env and restarts: a clean stop of the duplicate.
        final = dup.leave()
        m._deliver("bob", "alice", [final])
        removed_at = m.t
        del m.nodes["bob"]
        self.assertIsNone(m.store("alice").body("djp3"))
        m.run_to(removed_at + 600)
        self.assertIsNone(m.store("alice").body("djp3"))          # Bob's last version remembered
        m.run_to(removed_at + 603)
        self.assertIsNotNone(m.store("alice").body("djp3"))       # then Don's record is accepted again
        self.assertEqual(m.store("alice").body("djp3").incarnation, don.incarnation)


class DigestNeverReplacesAnOldLifeTest(unittest.TestCase):
    """Step 7, last paragraph: a digest entry 'I2, counter 3' reaching a peer
    that still holds an I1 body refreshes nothing; the next repair response
    delivers the I2 body."""

    def test(self):
        m = Mesh(start=0)
        m.start("alice", "alice")
        carol = m.start("carol", "carol")
        old = Record(grange="djp3", incarnation=I1, counter=65, rev=5, farm_age_s=0.0,
                     farm_stale=False, leaving=False, agents=tuple(two_sessions()))
        I2 = I1 + 620_000
        new = Record(grange="djp3", incarnation=I2, counter=3, rev=2, farm_age_s=0.0,
                     farm_stale=False, leaving=False, agents=tuple(two_sessions()))
        carol.store.apply_body(old)
        m.store("alice").apply_body(new)
        carol.store.apply_digest({"djp3": DigestEntry(I2, 3, 2, 0.0)})
        self.assertEqual(held(carol.store, "djp3"), (I1, 65, 5))
        m.repair("carol", "alice")
        self.assertEqual(held(carol.store, "djp3"), (I2, 3, 2))


if __name__ == "__main__":
    unittest.main()
