"""The ticker on a fake clock (R6.3, R6.4, R6.22)."""
import unittest

from grange.clock import FakeClock
from grange.model import Record
from grange.ticker import Ticker

from helpers import agent, make_store


class TickerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = make_store("djp3", self.clock)
        self.resumes = []
        self.ticker = Ticker(self.store, self.clock, on_resume=self.resumes.append, log=lambda m: None)

    def run_seconds(self, n):
        for _ in range(n):
            self.clock.advance(1)
            self.ticker.run_once()

    def test_flips_farm_stale_and_moves_the_cursor(self):
        self.store.set_agents([agent("a")])
        cursor = self.store.cursor
        self.run_seconds(59)
        self.assertFalse(self.store.farm_stale)
        self.assertEqual(self.store.cursor, cursor)
        self.run_seconds(1)
        self.assertTrue(self.store.farm_stale)
        self.assertGreater(self.store.cursor, cursor)

    def test_expires_peers(self):
        self.store.apply_body(Record(grange="alice", incarnation=1, counter=1, rev=1, farm_age_s=None,
                                     farm_stale=False, leaving=False, agents=()))
        self.run_seconds(60)
        self.assertIn("alice", self.store.peer_ids())
        self.run_seconds(1)
        self.assertNotIn("alice", self.store.peer_ids())

    def test_steady_ticks_are_not_a_resume(self):
        self.run_seconds(30)
        self.assertEqual(self.resumes, [])

    def test_a_gap_raises_the_resume_signal(self):
        self.run_seconds(3)
        self.clock.advance(5)                    # exactly the threshold: not a resume
        self.assertFalse(self.ticker.run_once())
        self.clock.advance(5.5)
        self.assertTrue(self.ticker.run_once())
        self.assertEqual(len(self.resumes), 1)
        self.assertAlmostEqual(self.resumes[0], 5.5)
        self.clock.advance(45 * 60)              # a long sleep with the stopwatch running
        self.assertTrue(self.ticker.run_once())
        self.assertEqual(self.ticker.resumes, 2)

    def test_resume_order_expires_before_signalling_and_flags_farm_after(self):
        order = []
        self.store.set_agents([agent("a")])
        self.store.apply_body(Record(grange="alice", incarnation=1, counter=1, rev=1, farm_age_s=None,
                                     farm_stale=False, leaving=False, agents=()))
        self.ticker.on_resume = lambda gap: order.append(("resume", self.store.peer_ids(), self.store.farm_stale))
        self.clock.advance(45 * 60)
        self.ticker.run_once()
        self.assertEqual(order, [("resume", [], False)])     # everyone expired first; farm flag after
        self.assertTrue(self.store.farm_stale)


if __name__ == "__main__":
    unittest.main()
