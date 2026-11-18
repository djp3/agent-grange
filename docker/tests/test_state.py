"""The Store (R10.1): merge, correction, cursor, expiry, farm_stale, caps."""
import threading
import time
import unittest

from grange.clock import FakeClock
from grange.config import MAX_BODIES_PER_MESSAGE, MAX_GOSSIP_BYTES, MAX_RECORDS
from grange.model import Agent, DigestEntry, Record, json_size
from grange.state import ACCEPTED, FORWARD, IGNORED, LOCAL, OVERFLOW, REFRESHED

from helpers import agent, board, make_store, push, repair


def body(gid="alice", inc=100, counter=1, rev=1, agents=(), leaving=False, farm_stale=False, farm_age_s=None):
    return Record(grange=gid, incarnation=inc, counter=counter, rev=rev, farm_age_s=farm_age_s,
                  farm_stale=farm_stale, leaving=leaving, agents=tuple(agents))


def held(store, gid):
    """(incarnation, counter, rev) as the store holds it, or None."""
    rec = store.body(gid)
    return None if rec is None else (rec.incarnation, rec.counter, rec.rev)


class ContentMergeTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = make_store("djp3", self.clock)

    def test_first_body_is_stored(self):
        self.assertEqual(self.s.apply_body(body(counter=5, rev=2)), ACCEPTED)
        self.assertEqual(held(self.s, "alice"), (100, 5, 2))

    def test_greater_rev_replaces_and_lower_is_ignored(self):
        self.s.apply_body(body(counter=5, rev=2, agents=[agent("a")]))
        self.assertEqual(self.s.apply_body(body(counter=6, rev=3, agents=[agent("b")])), ACCEPTED)
        self.assertEqual(board(self.s)["alice"]["agents"][0]["name"], "b")
        self.assertEqual(self.s.apply_body(body(counter=4, rev=2, agents=[agent("a")])), IGNORED)
        self.assertEqual(board(self.s)["alice"]["agents"][0]["name"], "b")

    def test_equal_content_version_is_a_tie_not_a_replacement(self):
        self.s.apply_body(body(counter=5, rev=2, agents=[agent("a")]))
        cursor = self.s.cursor
        self.assertEqual(self.s.apply_body(body(counter=5, rev=2, agents=[agent("other")])), IGNORED)
        self.assertEqual(self.s.cursor, cursor)
        # Same content version with a later counter refreshes liveness only.
        self.assertEqual(self.s.apply_body(body(counter=9, rev=2)), REFRESHED)
        self.assertEqual(held(self.s, "alice"), (100, 9, 2))
        self.assertEqual(board(self.s)["alice"]["agents"][0]["name"], "a")
        self.assertEqual(self.s.cursor, cursor)

    def test_greater_content_keeps_a_later_stored_counter(self):
        self.s.apply_body(body(counter=5, rev=2))
        self.s.apply_digest({"alice": DigestEntry(100, 14, 2, None)})
        self.s.apply_body(body(counter=10, rev=3))
        self.assertEqual(held(self.s, "alice"), (100, 14, 3))

    def test_restart_with_greater_incarnation_replaces_without_expiry(self):
        self.s.apply_body(body(inc=100, counter=66, rev=6))
        self.clock.advance(5)
        self.assertEqual(self.s.apply_body(body(inc=200, counter=2, rev=2)), ACCEPTED)
        self.assertEqual(held(self.s, "alice"), (200, 2, 2))      # liveness taken whole
        self.assertEqual(self.s.seen_age("alice"), 0.0)

    def test_older_incarnation_is_ignored_entirely(self):
        self.s.apply_body(body(inc=200, counter=2, rev=2))
        self.assertEqual(self.s.apply_body(body(inc=100, counter=999, rev=50)), IGNORED)
        self.assertEqual(held(self.s, "alice"), (200, 2, 2))


class LivenessMergeTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = make_store("djp3", self.clock)
        self.s.apply_body(body(inc=100, counter=5, rev=2, farm_age_s=1.0))

    def test_digest_refreshes_liveness_with_no_body(self):
        self.clock.advance(20)
        self.assertEqual(self.s.apply_digest({"alice": DigestEntry(100, 7, 2, 3.0)}), 1)
        self.assertEqual(held(self.s, "alice"), (100, 7, 2))
        self.assertEqual(self.s.seen_age("alice"), 0.0)
        self.assertEqual(board(self.s)["alice"]["farm_age_s"], 3.0)

    def test_digest_with_equal_or_lower_counter_refreshes_nothing(self):
        self.clock.advance(20)
        self.s.apply_digest({"alice": DigestEntry(100, 5, 2, 3.0)})
        self.s.apply_digest({"alice": DigestEntry(100, 4, 2, 3.0)})
        self.assertEqual(self.s.seen_age("alice"), 20.0)

    def test_digest_for_unknown_id_creates_nothing(self):
        cursor = self.s.cursor
        self.s.apply_digest({"bob": DigestEntry(1, 9, 9, None)})
        self.assertIsNone(self.s.body("bob"))
        self.assertEqual(self.s.cursor, cursor)

    def test_digest_with_other_incarnation_refreshes_nothing(self):
        self.clock.advance(20)
        self.s.apply_digest({"alice": DigestEntry(200, 3, 2, 0.0)})
        self.s.apply_digest({"alice": DigestEntry(50, 99, 2, 0.0)})
        self.assertEqual(held(self.s, "alice"), (100, 5, 2))
        self.assertEqual(self.s.seen_age("alice"), 20.0)

    def test_body_and_liveness_always_share_one_incarnation(self):
        steps = [
            lambda: self.s.apply_digest({"alice": DigestEntry(200, 3, 2, 0.0)}),
            lambda: self.s.apply_body(body(inc=100, counter=9, rev=2)),
            lambda: self.s.apply_body(body(inc=200, counter=2, rev=2)),
            lambda: self.s.apply_digest({"alice": DigestEntry(100, 50, 2, 0.0)}),
            lambda: self.s.apply_digest({"alice": DigestEntry(200, 4, 2, 0.0)}),
            lambda: self.s.apply_body(body(inc=100, counter=99, rev=9)),
        ]
        for step in steps:
            step()
            peer = self.s._peers["alice"]
            stamped = self.s.body("alice")
            self.assertEqual(peer.body.incarnation, stamped.incarnation)
            self.assertEqual(self.s.digest()["alice"].incarnation, peer.body.incarnation)
        self.assertEqual(held(self.s, "alice"), (200, 4, 2))


class OwnIdTest(unittest.TestCase):
    def test_equal_or_lower_version_of_own_id_is_ignored(self):
        clock = FakeClock()
        s = make_store("djp3", clock)
        inc = s.incarnation
        s.bump()
        s.apply_digest({"djp3": DigestEntry(inc, 2, 1, None)})       # our echo
        s.apply_digest({"djp3": DigestEntry(inc, 1, 1, None)})       # lag
        s.apply_body(body(gid="djp3", inc=inc - 5, counter=50))      # an older life
        self.assertEqual(s.incarnation, inc)
        self.assertEqual(s.corrections, 0)

    def test_correction_after_a_backward_clock_step(self):
        clock = FakeClock(wall=2_000_000.0)
        s = make_store("djp3", clock)
        old = 2_000_000_000 + 3_600_000              # an earlier life an hour in the future
        s.apply_body(body(gid="djp3", inc=old, counter=5, rev=2))
        self.assertEqual(s.incarnation, old + 1)
        self.assertEqual((s.counter, s.rev), (1, 1))
        self.assertEqual(s.corrections, 1)
        self.assertFalse(s.duplicate_id)
        self.assertEqual([e.kind for e in s.events], [LOCAL])        # pushed like a content change
        self.assertEqual(s.events[0].record.incarnation, old + 1)

    def test_correction_uses_the_clock_when_it_is_later(self):
        clock = FakeClock(wall=2_000_000.0)
        s = make_store("djp3", clock)
        clock.advance(10)
        s.apply_digest({"djp3": DigestEntry(s.incarnation + 1, 1, 1, None)})
        self.assertEqual(s.incarnation, 2_000_010_000)

    def test_one_message_corrects_once(self):
        clock = FakeClock(wall=2_000_000.0)
        s = make_store("djp3", clock)
        a = make_store("alice", clock)
        old = s.incarnation + 3_600_000
        a.apply_body(body(gid="djp3", inc=old, counter=5, rev=2))
        repair(s, a)                     # body and digest entry for our id arrive together
        self.assertEqual(s.corrections, 1)
        self.assertFalse(s.duplicate_id)
        self.assertEqual(held(a, "djp3"), (old + 1, 1, 1))          # follow-up push healed the peer


class DuplicateIdTest(unittest.TestCase):
    def test_two_stores_with_one_id_settle(self):
        clock = FakeClock(wall=2_000_000.0)
        don = make_store("djp3", clock)
        clock.advance(30)
        bob = make_store("djp3", clock)          # a copied .env: a greater incarnation
        alice = make_store("alice", clock)
        floods = 0
        for _ in range(20):                      # many rounds through a shared peer
            clock.advance(3)
            don.bump(), bob.bump()
            repair(don, alice, "don-addr", "alice-addr")
            repair(bob, alice, "bob-addr", "alice-addr")
            floods = don.corrections + bob.corrections
        self.assertLessEqual(floods, 3)
        self.assertEqual((don.corrections, bob.corrections), (1, 1))
        self.assertTrue(don.duplicate_id)
        self.assertFalse(bob.duplicate_id)        # the winner's flag never sets
        self.assertEqual(held(alice, "djp3")[0], bob.incarnation)   # the mesh shows the winner
        self.assertTrue(any("duplicate grange id" in line for line in don.log))

    def test_flag_clears_after_forget_seconds_without_a_foreign_version(self):
        clock = FakeClock(wall=2_000_000.0)
        s = make_store("djp3", clock)
        s.apply_digest({"djp3": DigestEntry(s.incarnation + 10, 1, 1, None)})
        s.apply_digest({"djp3": DigestEntry(s.incarnation + 10, 1, 1, None)})
        self.assertTrue(s.duplicate_id)
        clock.advance(300)
        s.apply_digest({"djp3": DigestEntry(s.incarnation + 10, 1, 1, None)})   # still foreign: flag stays
        clock.advance(600)
        s.expire()
        self.assertTrue(s.duplicate_id)
        clock.advance(1)
        s.expire()
        self.assertFalse(s.duplicate_id)
        self.assertEqual(s.corrections, 1)


class ExpiryAndForgetTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = make_store("djp3", self.clock)
        self.s.apply_body(body(inc=100, counter=5, rev=2, agents=[agent("a", owner="alice")]))

    def test_seen_age_equal_to_expire_is_kept_greater_expires(self):
        self.clock.advance(60)
        self.assertEqual(self.s.expire(), [])
        cursor = self.s.cursor
        self.clock.advance(0.001)
        self.assertEqual(self.s.expire(), ["alice"])
        self.assertGreater(self.s.cursor, cursor)
        self.assertNotIn("alice", board(self.s))
        self.assertEqual(self.s.remembered("alice"), (100, 5))

    def test_lagging_copy_is_refused_after_removal(self):
        self.clock.advance(61)
        self.s.expire()
        self.assertEqual(self.s.apply_body(body(inc=100, counter=5, rev=2)), IGNORED)
        self.assertEqual(self.s.apply_body(body(inc=100, counter=4, rev=2)), IGNORED)
        self.assertNotIn("alice", board(self.s))

    def test_digest_entry_never_readmits(self):
        self.clock.advance(61)
        self.s.expire()
        self.s.apply_digest({"alice": DigestEntry(100, 500, 9, 0.0)})
        self.assertNotIn("alice", board(self.s))

    def test_relayed_body_with_later_counter_readmits(self):
        self.clock.advance(61)
        self.s.expire()
        cursor = self.s.cursor
        # Bob relays alice's same rev-2 body, stamped with his later stored counter (4.2).
        self.assertEqual(self.s.apply_body(body(inc=100, counter=6, rev=2), source="bob-addr"), ACCEPTED)
        self.assertIn("alice", board(self.s))
        self.assertGreater(self.s.cursor, cursor)
        self.assertEqual(self.s.events[-1].kind, FORWARD)             # readmission is forwarded
        self.assertEqual(self.s.events[-1].source, "bob-addr")

    def test_forget_map_ages_out(self):
        self.clock.advance(61)
        self.s.expire()
        self.clock.advance(600)
        self.s.expire()
        self.assertIsNotNone(self.s.remembered("alice"))
        self.clock.advance(1)
        self.s.expire()
        self.assertIsNone(self.s.remembered("alice"))
        self.assertEqual(self.s.apply_body(body(inc=100, counter=1, rev=1)), ACCEPTED)

    def test_accepted_leaving_removes_at_once(self):
        cursor = self.s.cursor
        self.assertEqual(self.s.apply_body(body(inc=100, counter=6, rev=3, leaving=True)), ACCEPTED)
        self.assertNotIn("alice", board(self.s))
        self.assertGreater(self.s.cursor, cursor)
        self.assertEqual(self.s.remembered("alice"), (100, 6))
        self.assertTrue(self.s.events[-1].record.leaving)              # forwarded once

    def test_leaving_from_an_older_incarnation_removes_nothing(self):
        self.s.apply_body(body(inc=200, counter=2, rev=2))
        self.assertEqual(self.s.apply_body(body(inc=100, counter=70, rev=7, leaving=True)), IGNORED)
        self.assertIn("alice", board(self.s))

    def test_liveness_refresh_postpones_expiry(self):
        self.clock.advance(50)
        self.s.apply_digest({"alice": DigestEntry(100, 6, 2, 0.0)})
        self.clock.advance(50)
        self.assertEqual(self.s.expire(), [])


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = make_store("djp3", self.clock)

    def assertMoves(self, fn, moves=True):
        before = self.s.cursor
        fn()
        (self.assertGreater if moves else self.assertEqual)(self.s.cursor, before)

    def test_events_that_move_the_cursor(self):
        self.assertMoves(lambda: self.s.set_agents([agent("a")]))
        self.assertMoves(lambda: self.s.apply_body(body(counter=1, rev=1)))         # a peer's first body
        self.assertMoves(lambda: self.s.apply_body(body(counter=2, rev=2)))         # new content
        self.clock.advance(61)
        self.assertMoves(self.s.expire)                                             # expiry (and farm silence)
        self.assertMoves(self.s.evaluate_farm_stale)                               # farm_stale flips

    def test_events_that_do_not(self):
        self.s.apply_body(body(counter=1, rev=1))
        self.assertMoves(self.s.bump, moves=False)
        self.assertMoves(lambda: self.s.apply_digest({"alice": DigestEntry(100, 5, 1, None)}), moves=False)
        self.assertMoves(lambda: self.s.apply_body(body(counter=6, rev=1)), moves=False)
        self.clock.advance(5)
        self.assertMoves(self.s.picture, moves=False)                               # ages tick
        self.assertMoves(self.s.expire, moves=False)
        self.s.set_agents([agent("a")])
        self.assertMoves(lambda: self.s.set_agents([agent("a")]), moves=False)      # identical push

    def test_only_after_equal_to_cursor_waits(self):
        cursor = self.s.cursor
        for after in (None, 0, cursor - 1, cursor + 1, cursor + 1000):
            started = time.monotonic()
            self.s.wait_for(after, timeout=5)
            self.assertLess(time.monotonic() - started, 0.5, after)
        started = time.monotonic()
        self.s.wait_for(cursor, timeout=0.2)
        self.assertGreaterEqual(time.monotonic() - started, 0.19)

    def test_wait_returns_when_the_cursor_moves(self):
        cursor = self.s.cursor
        threading.Timer(0.05, lambda: self.s.set_agents([agent("a")])).start()
        started = time.monotonic()
        picture = self.s.wait_for(cursor, timeout=5)
        self.assertLess(time.monotonic() - started, 1)
        self.assertNotEqual(picture["cursor"], cursor)
        self.assertEqual(picture["granges"][0]["agents"][0]["name"], "a")


class FarmTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = make_store("djp3", self.clock)

    def test_never_pushed_goes_stale_after_the_grace(self):
        self.assertIsNone(board(self.s)["djp3"]["farm_age_s"])
        self.clock.advance(59.9)
        self.assertFalse(self.s.evaluate_farm_stale())
        self.clock.advance(0.1)
        self.assertTrue(self.s.evaluate_farm_stale())
        self.assertTrue(board(self.s)["djp3"]["farm_stale"])
        self.assertIsNone(board(self.s)["djp3"]["farm_age_s"])
        self.assertEqual((self.s.rev, self.s.counter), (2, 2))
        self.assertEqual(self.s.events[-1].kind, LOCAL)

    def test_stale_keeps_the_agents(self):
        self.s.set_agents([agent("a")])
        self.clock.advance(60)
        self.s.evaluate_farm_stale()
        self.assertEqual(len(board(self.s)["djp3"]["agents"]), 1)
        self.assertEqual(board(self.s)["djp3"]["farm_age_s"], 60.0)

    def test_identical_push_while_stale_is_a_content_change(self):
        self.s.set_agents([agent("a")])
        self.clock.advance(60)
        self.s.evaluate_farm_stale()
        rev, cursor = self.s.rev, self.s.cursor
        self.assertTrue(self.s.set_agents([agent("a")]))
        self.assertEqual(self.s.rev, rev + 1)
        self.assertGreater(self.s.cursor, cursor)
        self.assertFalse(board(self.s)["djp3"]["farm_stale"])

    def test_identical_push_otherwise_only_records_the_time(self):
        self.s.set_agents([agent("a")])
        self.clock.advance(20)
        rev, counter, events = self.s.rev, self.s.counter, len(self.s.events)
        self.assertFalse(self.s.set_agents([agent("a")]))
        self.assertEqual((self.s.rev, self.s.counter, len(self.s.events)), (rev, counter, events))
        self.assertEqual(board(self.s)["djp3"]["farm_age_s"], 0.0)

    def test_reorder_only_push_is_not_a_change_and_keeps_order(self):
        self.s.set_agents([agent("a"), agent("b")])
        rev = self.s.rev
        self.assertFalse(self.s.set_agents([agent("b"), agent("a")]))
        self.assertEqual(self.s.rev, rev)
        self.assertEqual([a["name"] for a in board(self.s)["djp3"]["agents"]], ["a", "b"])

    def test_same_named_agents_are_both_kept(self):
        self.s.set_agents([agent("a"), agent("a")])
        self.assertEqual(len(board(self.s)["djp3"]["agents"]), 2)
        self.assertTrue(self.s.set_agents([agent("a")]))       # one fewer is a change

    def test_unseen_state_is_relayed_unchanged(self):
        self.s.set_agents([Agent("djp3", "n", "contemplating")])
        self.assertEqual(self.s.own_body().agents[0].state, "contemplating")

    def test_heartbeat_moves_only_the_counter(self):
        self.s.bump()
        self.assertEqual((self.s.counter, self.s.rev), (2, 1))
        self.assertEqual(self.s.events, [])


class PictureTest(unittest.TestCase):
    def test_local_first_then_by_id(self):
        clock = FakeClock()
        s = make_store("mike", clock)
        for gid in ("zed", "alice", "bob"):
            s.apply_body(body(gid=gid))
        self.assertEqual([g["grange"] for g in s.picture()["granges"]], ["mike", "alice", "bob", "zed"])

    def test_ages(self):
        clock = FakeClock()
        s = make_store("djp3", clock)
        s.apply_body(body(farm_age_s=95.0, farm_stale=True))
        clock.advance(2.4)
        g = board(s)["alice"]
        self.assertEqual((g["seen_age_s"], g["farm_age_s"], g["farm_stale"]), (2.4, 97.4, True))
        self.assertEqual(board(s)["djp3"]["seen_age_s"], 0.0)
        top = s.picture()
        self.assertEqual((top["grange"], top["incarnation"]), ("djp3", s.incarnation))


class RepairConstructionTest(unittest.TestCase):
    def test_bodies_carry_the_senders_stored_liveness(self):
        clock = FakeClock()
        don, alice, bob = make_store("djp3", clock), make_store("alice", clock), make_store("bob", clock)
        don.set_agents([agent("a")])
        push(don, alice)                                   # alice holds rev 2, counter 2
        for _ in range(5):
            don.bump()
        alice.apply_digest(don.digest())                   # alice's stored counter climbs to 7
        clock.advance(2)
        request = bob.repair_request()
        response = alice.answer_repair(request)
        relayed = {r.grange: r for r in response.records}["djp3"]
        self.assertEqual((relayed.counter, relayed.rev), (7, 2))
        self.assertEqual(relayed.farm_age_s, 2.0)          # aged by the time alice held it
        self.assertIn("alice", {r.grange for r in response.records})
        self.assertEqual(set(response.digest), {"alice", "djp3"})

    def test_response_lists_only_newer_or_missing_and_followup_pushes_back(self):
        clock = FakeClock()
        a, b = make_store("alice", clock), make_store("bob", clock)
        c = make_store("carol", clock)
        push(c, a)
        a.set_agents([agent("x", owner="alice")])
        b.set_agents([agent("y", owner="bob")])
        push(a, b)                                          # b already holds a's current body
        response = a.answer_repair(b.repair_request())
        self.assertEqual({r.grange for r in response.records}, {"carol"})
        followup = b.take_repair_response(response)
        self.assertEqual({r.grange for r in followup}, {"bob"})
        a.take_push(b.push_message(followup))
        self.assertEqual(set(board(a)), {"alice", "bob", "carol"})
        self.assertEqual(set(board(b)), {"alice", "bob", "carol"})

    def test_outgoing_bodies_are_trimmed_to_the_caps(self):
        clock = FakeClock()
        s = make_store("djp3", clock)
        big = [Agent("o" * 256, "n" * 256, "s" * 256)] * 200
        for i in range(250):
            s.apply_body(body(gid=f"g{i:03}", agents=big if i < 10 else ()))
        msg = s.trim(s.digest(), s.bodies_missing_from({}))
        self.assertLessEqual(len(msg.records), MAX_BODIES_PER_MESSAGE)
        self.assertLessEqual(json_size(msg.to_json()), MAX_GOSSIP_BYTES)
        self.assertEqual(msg.records[0].grange, "djp3")    # own record first
        self.assertGreater(len(msg.records), 100)

    def test_record_cap_counts_the_own_record(self):
        clock = FakeClock()
        s = make_store("djp3", clock)
        for i in range(MAX_RECORDS - 1):
            self.assertEqual(s.apply_body(body(gid=f"g{i}")), ACCEPTED)
        self.assertEqual(s.apply_body(body(gid="one-too-many")), OVERFLOW)
        self.assertEqual(s.overflow, 1)
        self.assertEqual(len(s.digest()), MAX_RECORDS)
        s.apply_body(body(gid="g0", counter=2, rev=2))     # known ids still update
        self.assertEqual(held(s, "g0"), (100, 2, 2))


if __name__ == "__main__":
    unittest.main()
