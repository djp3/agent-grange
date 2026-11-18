"""Replay of the peer discovery and topology examples in requirements 6.4.

A team of twelve at 100.64.0.1 to 100.64.0.12, Don (djp3) at .5 and Alice at
.6; Carol joins at .13. Defaults apply. Each grange is the real Store, gossip
core, and gossip handler in the simulation harness. Example 8, the untagged
device, needs a tailnet and is covered by plan phase 6. A claim this replay
contradicts is to be corrected in the requirements.
"""
from __future__ import annotations

import unittest

from grange.gossip import choose_neighbours

from sim.harness import Sim

NAMES = {1: "bob", 2: "erin", 3: "gus", 4: "hana", 5: "djp3", 6: "alice", 7: "ivan", 8: "judy",
         9: "ken", 10: "lena", 11: "mia", 12: "noor"}


def ip(i):
    return f"100.64.0.{i}:7789"


def team(hint_lag=180.0, frank_among_dons_four=True):
    """The twelve, settled. Frank is one of Don's four hash-ranked neighbours
    in the thirteen-grange team, so example 5 has Don pushing to him."""
    names = dict(NAMES)
    thirteen = [ip(i) for i in range(1, 14) if i != 5]
    ranked = choose_neighbours("djp3", ip(5), thirteen, 5)
    frank = int(ranked[1].split(".")[3].split(":")[0])
    names[frank], names[3] = "frank", names[frank] if frank != 3 else "frank"
    sim = Sim(0, hint_lag=hint_lag)
    for i in range(1, 13):
        sim.add(names[i], ip(i), start=False)
    for i in range(1, 13):
        sim.start(sim.nodes[ip(i)])
    sim.run_for(35)                         # past everyone's first discovery refresh
    return sim, {n.gid: n for n in sim.nodes.values()}


class CarolJoinsTest(unittest.TestCase):
    """Example 1: a new teammate joins."""

    def test(self):
        sim, g = team()
        before = {n.gid: n.core.neighbours() for n in sim.nodes.values()}
        carol = sim.add("carol", ip(13))
        self.assertEqual(len(carol.core.discovered), 12)         # discovers the twelve at startup
        sim.run_for(2)
        self.assertEqual(len([a for a in sim.attempts if a[1] == carol.addr and a[3] == "repair"]), 5)
        self.assertEqual(len(carol.board()), 13)                  # full board within a few seconds
        sim.run_for(30)                                           # every other grange's refresh lists her
        unbound = []
        for n in sim.nodes.values():
            if n is carol:
                continue
            self.assertTrue(n.core.discovered.get(carol.addr), n.gid)
            self.assertIn(carol.addr, n.core.live())
            if n.core.binding.get(carol.addr) is None:
                unbound.append(n.gid)                             # live by the online hint alone (case b)
        self.assertTrue(unbound)                                  # some hold her record, never talked to her
        self.assertEqual(sim.nodes[ip(12)].core.neighbours()[0], carol.addr)   # successor of 100.64.0.12
        for gid, old in before.items():
            new = g[gid].core.neighbours()
            self.assertLessEqual(len(set(old) - set(new)), 1, gid)
            if carol.addr in new[1:]:
                self.assertEqual(len(set(old) - set(new)), 1, gid)     # displaces exactly one
        self.assertEqual(len(carol.board()), 13)


class BindingTest(unittest.TestCase):
    """Example 2: binding an address to a name."""

    def test(self):
        sim, g = team()
        carol = sim.add("carol", ip(13))
        sim.run_for(40)
        don = g["djp3"]
        don.core.repair(carol.addr)                               # Don's grange first talks to .13
        sim.run_for(0.2)
        self.assertEqual(don.core.binding.get(carol.addr), "carol")   # the reply says from: carol
        # Bob set GRANGE_ID=bob but renamed his sidecar grange-robert.
        sim.hostnames[ip(1)] = "grange-robert"
        sim.hostnames[carol.addr] = "grange-carol"
        bob = g["bob"]
        bob.gid = "bob"
        sim.restart(bob)
        sim.run_for(40)
        for n in sim.nodes.values():
            if n.core.binding.get(ip(1)) == "bob":
                said = [m for _, m in n.logs if "grange-robert" in m or "'robert'" in m]
                self.assertEqual(len(said), 1, n.gid)             # logged once
        self.assertEqual(don.core.binding.get(ip(1)), "bob")      # goes by what his records say


class DonsNeighboursTest(unittest.TestCase):
    """Example 3: how Don's neighbours are chosen."""

    def setUp(self):
        self.live = [ip(i) for i in range(1, 14) if i != 5]

    def test(self):
        nbrs = choose_neighbours("djp3", ip(5), self.live, 5)
        self.assertEqual(nbrs[0], ip(6))                          # Alice is his successor
        self.assertEqual(choose_neighbours("djp3", ip(5), self.live, 5), nbrs)   # same five tomorrow
        alice_live = [ip(i) for i in range(1, 14) if i != 6]
        alice = choose_neighbours("alice", ip(6), alice_live, 5)
        self.assertNotEqual(set(nbrs[1:]), set(alice[1:]))        # her four are different

    def test_a_ranked_neighbour_leaves(self):
        nbrs = choose_neighbours("djp3", ip(5), self.live, 5)
        frank = nbrs[2]
        after = choose_neighbours("djp3", ip(5), [a for a in self.live if a != frank], 5)
        self.assertEqual(after[0], nbrs[0])
        self.assertEqual(set(nbrs[1:]) - {frank}, set(after[1:]) & set(nbrs[1:]))   # the other three stay
        self.assertEqual(len(set(after) - set(nbrs)), 1)          # the fifth-ranked moves up

    def test_the_successor_leaves(self):
        after = choose_neighbours("djp3", ip(5), [a for a in self.live if a != ip(6)], 5)
        self.assertEqual(after[0], ip(7))                         # the next address in order

    def test_successor_edges_make_one_ring(self):
        everyone = [ip(i) for i in range(1, 14)]
        cur, seen = ip(5), []
        for _ in everyone:
            seen.append(cur)
            cur = choose_neighbours("x", cur, [a for a in everyone if a != cur], 5)[0]
        self.assertEqual(sorted(seen), sorted(everyone))


class RepairPeerTest(unittest.TestCase):
    """Example 4: who gets the repairs."""

    def test(self):
        sim, g = team()
        sim.add("carol", ip(13))
        sim.run_for(35)
        don = g["djp3"]
        live, nbrs = set(don.core.live()), set(don.core.neighbours())
        self.assertEqual(len(live - nbrs), 7)
        picks = {don.core.repair_peer() for _ in range(300)}
        self.assertEqual(picks, live - nbrs)


class LaptopClosesTest(unittest.TestCase):
    """Example 5: a laptop closes; headscale reports it online a few minutes more."""

    def test(self):
        sim, g = team(hint_lag=180)
        sim.add("carol", ip(13))
        sim.run_for(35)
        don, frank = g["djp3"], g["frank"]
        self.assertIn(frank.addr, don.core.neighbours())
        t0 = sim.now
        sim.kill(frank)
        sim.change(don)                                           # Don's next push to him
        sim.run_for(1.01)
        first = [a for a in sim.attempts if a[1] == don.addr and a[2] == frank.addr and a[0] >= t0]
        self.assertEqual([(a[3], a[4]) for a in first], [("push", False)])
        self.assertIn(frank.addr, don.core.failed)
        self.assertNotIn(frank.addr, don.core.live())
        self.assertNotIn(frank.addr, don.core.neighbours())
        sim.run_for(75)
        for n in sim.up_nodes():
            self.assertNotIn("frank", n.board())                  # expired after 60 s without a heartbeat
        sim.run_until(t0 + 170)
        wasted = [a for a in sim.attempts if a[1] == don.addr and a[2] == frank.addr and t0 + 2 <= a[0]]
        self.assertTrue(all(not ok for *_, ok in wasted))
        self.assertLessEqual(len(wasted), 170 // 30 + 1)          # at most one wasted second per 30 s
        sim.run_until(t0 + 215)                                   # the hint has turned offline
        self.assertNotIn(frank.addr, don.core.live())
        start = len(sim.attempts)
        sim.run_until(t0 + 335)
        probes = [a for a in sim.attempts[start:] if a[1] == don.addr and a[2] == frank.addr]
        self.assertGreaterEqual(len(probes), 3)                   # the probe still tries, every 30 s
        self.assertLessEqual(len(probes), 5)


class SplitTest(unittest.TestCase):
    """Example 6: the team splits in two, while both halves reach headscale."""

    def split(self, withhold_one_half=False):
        sim, g = team()
        sim.add("carol", ip(13))
        sim.run_for(35)
        sim.kill(g["frank"])
        sim.run_for(200)                                          # frank gone, his hint offline
        running = [n for n in sim.nodes.values() if n.gid != "frank"]
        office, home = running[:6], running[6:]
        sim.cut(office, home)
        if withhold_one_half:
            for n in home:
                n.hints_withheld = True
        sim.run_for(80)
        for n in running:
            self.assertEqual(len(n.board()), 6, n.gid)            # each half expired the other
        healed = sim.now
        sim.heal()
        done_at = None
        while sim.now < healed + 45:
            sim.run_for(1)
            if all(len(n.board()) == 12 for n in running):
                done_at = sim.now - healed
                break
        self.assertIsNotNone(done_at)                             # within 30 plus 15 s at worst
        return done_at

    def test_hints_keep_the_far_half_live(self):
        self.split()

    def test_the_probe_finds_the_far_half_when_one_side_lost_headscale(self):
        self.split(withhold_one_half=True)


class CutOffTest(unittest.TestCase):
    """Example 7: Don's network drops for ten minutes."""

    def test(self):
        sim, g = team()
        sim.add("carol", ip(13))
        sim.run_for(35)
        sim.kill(g["frank"])
        sim.run_for(200)
        don = g["djp3"]
        others = [n for n in sim.nodes.values() if n is not don and n.gid != "frank"]
        t0 = sim.now
        sim.cut([don], others)
        sim.run_for(70)
        self.assertEqual(don.board(), {"djp3"})                    # everyone expired on his board
        mark = len(sim.attempts)
        sim.run_until(t0 + 600)
        tries = [a for a in sim.attempts[mark:] if a[1] == don.addr]
        rate = len(tries) / (sim.now - t0 - 70)
        self.assertGreater(rate, 0.25)                            # one attempt every 3 s
        self.assertLess(rate, 0.5)
        self.assertTrue(all(not ok for *_, ok in tries))
        sim.heal()
        back = None
        for _ in range(10):
            sim.run_for(1)
            if len(don.board()) == 12 and all(don.gid in n.board() for n in others):
                back = sim.now - (t0 + 600)
                break
        self.assertIsNotNone(back)                                # refilled within a few seconds
        self.assertLessEqual(back, 5)


class StaticPeersTest(unittest.TestCase):
    """Example 9: local testing without a tailnet; every listed address counts as online."""

    def test(self):
        sim, g = team(hint_lag=10 ** 9)                           # the hint never turns offline
        don, dead = g["djp3"], g["ivan"]
        t0 = sim.now
        sim.kill(dead)
        sim.run_for(50)
        self.assertIn(dead.gid, don.board())                      # not yet expired
        sim.run_for(26)
        for n in sim.up_nodes():
            self.assertNotIn(dead.gid, n.board())                 # its record expires after 60 s
        sim.run_for(120)                                          # marked, re-enters at a refresh, fails again
        attempts = [a for a in sim.attempts if a[2] == dead.addr and a[0] > t0 + 76]
        self.assertTrue(attempts)
        self.assertTrue(all(not ok for *_, ok in attempts))


if __name__ == "__main__":
    unittest.main()
