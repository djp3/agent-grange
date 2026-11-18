"""The simulation at design size (R10.5): 100 granges of 10 agents.

`make test` runs only the short all-alive run, a few simulated minutes.
`make test-sim` sets GRANGE_SIM=long and runs the five hour-long runs.
No assertion is weakened to fit a time limit.

Bounds, from the requirements:
  convergence      a change reaches every live grange within 5 s (R6.14)
  traffic          under 10 KB/s per grange, sent plus received, averaged over
                   each minute (R6.15); the harness adds 150 bytes of HTTP
                   overhead to every request and response
  no false expiry  no live grange is ever expired by another live grange
  rejoin           DISCOVERY_SECONDS plus 15 s after a partition heals
  return           a grange silent past EXPIRE_SECONDS regains the full picture
                   within 10 s and is back on every other board within 10 s;
                   a fresh start has the full picture within 10 s
"""
from __future__ import annotations

import os
import time
import unittest

from sim.harness import PAUSED, Sim

N = 100
LATENCY = 0.05                    # per hop, seconds
BUDGET = 10_000                   # bytes per second
HOUR = 3600.0
WARMUP = 120.0                    # startup fetches every body once; the budget is for the settled mesh
LONG = os.environ.get("GRANGE_SIM") == "long"


def minutes(t_from: float, t_to: float) -> list:
    return list(range(int(t_from // 60), int(t_to // 60)))


class DesignSizeMixin:
    def build(self, seed=1, **kw):
        started = time.monotonic()
        sim = Sim(N, latency=LATENCY, seed=seed, **kw)
        sim.run_for(WARMUP)
        self.assertEqual({len(n.board()) for n in sim.nodes.values()}, {N})
        self.wall_started = started
        return sim

    def report(self, name: str, sim: Sim) -> None:
        wall = time.monotonic() - self.wall_started
        print(f"\n[sim] {name}: {sim.now:.0f} s simulated in {wall:.1f} s wall", flush=True)

    def changes(self, sim: Sim, until: float, every: float, pool=None) -> list:
        """A change on a random grange every `every` seconds until `until`;
        each checked for convergence 10 s later."""
        out = []
        while sim.now + every <= until:
            sim.run_for(every)
            alive = pool() if pool else sim.up_nodes()
            ch = sim.change(sim.rng.choice(alive))
            out.append((ch, alive))
        sim.run_for(10)
        return out

    def assert_converged(self, results, bound=5.0) -> float:
        worst = 0.0
        for ch, alive in results:
            t = None
            # the nodes alive at the change and still alive now
            nodes = [n for n in alive if n.state == "up"]
            t = sim_convergence(ch, nodes)
            self.assertIsNotNone(t, f"change {ch} never reached every live grange")
            worst = max(worst, t)
        self.assertLessEqual(worst, bound)
        return worst


def sim_convergence(ch, nodes):
    gid, inc, rev, t0 = ch
    worst = 0.0
    for n in nodes:
        if n.gid == gid:
            continue
        t = n.accepted.get((gid, inc, rev))
        if t is None:
            if not n.holds(gid, rev, inc):
                return None
            continue
        worst = max(worst, t - t0)
    return worst


class ShortRunTest(DesignSizeMixin, unittest.TestCase):
    """A few simulated minutes, all alive: the smoke test in `make test`."""

    def test_all_alive_short(self):
        sim = self.build()
        results = self.changes(sim, until=WARMUP + 120, every=15)
        worst = self.assert_converged(results)
        self.assertEqual(sim.false_expiries(sim.up_nodes()), [])
        rate = sim.max_rate(minutes=minutes(WARMUP, sim.now))
        self.assertLess(rate, BUDGET)
        self.report(f"short all-alive: worst convergence {worst:.2f} s, worst rate {rate:.0f} B/s", sim)


@unittest.skipUnless(LONG, "hour-long simulation; run with `make test-sim`")
class HourLongTest(DesignSizeMixin, unittest.TestCase):

    def test_all_alive(self):
        sim = self.build(seed=11)
        results = self.changes(sim, until=HOUR, every=60)
        worst = self.assert_converged(results)
        self.assertEqual(sim.false_expiries(sim.up_nodes()), [])
        rate = sim.max_rate(minutes=minutes(WARMUP, sim.now))
        self.assertLess(rate, BUDGET)
        self.report(f"all alive: {len(results)} changes, worst convergence {worst:.2f} s, "
                    f"worst minute {rate:.0f} B/s", sim)

    def test_half_dead(self):
        sim = self.build(seed=12, hint_lag=120)
        dead = sim.rng.sample(list(sim.nodes.values()), N // 2)
        for n in dead:
            sim.kill(n)
        live = sim.up_nodes()
        sim.run_for(180)                     # the dead expire everywhere; their hints turn offline
        start = sim.now
        results = self.changes(sim, until=HOUR, every=60)
        worst = self.assert_converged(results)
        self.assertEqual(sim.false_expiries(live), [])
        rate = sim.max_rate(nodes=live, minutes=minutes(start, sim.now))
        self.assertLess(rate, BUDGET)
        self.report(f"half dead: worst convergence {worst:.2f} s, worst minute {rate:.0f} B/s", sim)

    def test_partition_and_rejoin(self):
        sim = self.build(seed=13)
        nodes = list(sim.nodes.values())
        worst = 0.0
        while sim.now + 600 <= HOUR:
            order = sim.rng.sample(nodes, N)
            a, b = order[: N // 2], order[N // 2:]
            sim.cut(a, b)
            sim.run_for(120)                 # past EXPIRE_SECONDS: each half expired the other
            self.assertTrue(all(len(n.board()) == N // 2 for n in nodes))
            healed = sim.now
            sim.heal()
            done = None
            while sim.now <= healed + 45 + 1:
                sim.run_for(1)
                if all(len(n.board()) == N for n in nodes):
                    done = sim.now - healed
                    break
            self.assertIsNotNone(done, "the halves did not converge")
            self.assertLessEqual(done, sim.cfg.discovery_s + 15)
            worst = max(worst, done)
            sim.run_for(600 - 120 - done)
        self.report(f"partition and rejoin: worst rejoin {worst:.0f} s", sim)

    def test_return_and_fresh_start(self):
        sim = self.build(seed=14)
        worst_back = worst_full = worst_fresh = 0.0
        paused = set()
        while sim.now + 300 <= HOUR:
            node = sim.rng.choice(sim.up_nodes())
            paused.add(node.gid)
            sim.pause(node)
            sim.run_for(90)                  # silent past EXPIRE_SECONDS
            others = [n for n in sim.up_nodes() if n is not node]
            self.assertTrue(all(node.gid not in n.board() for n in others))
            t0 = sim.now
            sim.unpause(node)
            full = back = None
            while sim.now <= t0 + 11:
                sim.run_for(0.25)
                if full is None and len(node.board()) == len(sim.up_nodes()):
                    full = sim.now - t0
                if back is None and all(node.gid in n.board() for n in others):
                    back = sim.now - t0
                if full is not None and back is not None:
                    break
            self.assertIsNotNone(full, "the returning grange did not regain the full picture")
            self.assertIsNotNone(back, "the returning grange is not back on every board")
            self.assertLessEqual(full, 10)
            self.assertLessEqual(back, 10)
            worst_back, worst_full = max(worst_back, back), max(worst_full, full)

            fresh = sim.add()                # a freshly started grange
            t1 = sim.now
            got = None
            while sim.now <= t1 + 11:
                sim.run_for(0.25)
                if len(fresh.board()) == len(sim.up_nodes()):
                    got = sim.now - t1
                    break
            self.assertIsNotNone(got, "the fresh start did not get the full picture")
            self.assertLessEqual(got, 10)
            worst_fresh = max(worst_fresh, got)
            sim.run_for(200)
        steady = [n for n in sim.up_nodes() if n.gid not in paused]     # the paused ones were meant to expire
        self.assertEqual(sim.false_expiries(steady), [])
        self.report(f"return and fresh start: worst full picture {worst_full:.2f} s, back on boards "
                    f"{worst_back:.2f} s, fresh start {worst_fresh:.2f} s", sim)

    def test_duplicate_id(self):
        """A flood is a new version of the shared id spreading across the mesh.
        After the mesh settles, the flagged grange still receives the winner's
        body as the follow-up push of each repair a peer runs with it (R5.10),
        about one small push every REPAIR_SECONDS; that trickle is bounded too."""
        sim = self.build(seed=15)
        original = list(sim.nodes.values())[17]
        gid = original.gid
        started = sim.now
        dup = sim.add(gid=gid, wall_offset=30.0)             # a copied .env; its clock reads later
        sim.run_for(HOUR - sim.now)
        corrections = original.store.corrections + dup.store.corrections
        self.assertGreaterEqual(corrections, 1)
        self.assertLessEqual(corrections, 3)                 # bounded
        flags = [original.store.duplicate_id, dup.store.duplicate_id]
        self.assertEqual(sorted(flags), [False, True])       # one flagged, one visible
        winner = dup if flags[1] is False else original
        others = [n for n in sim.nodes.values() if n not in (original, dup)]
        incs = {n.store.body(gid).incarnation for n in others if n.store.body(gid)}
        self.assertEqual(incs, {winner.store.incarnation})  # the mesh settled on one of them
        versions = {(inc, rev): t for n in others for (g, inc, rev), t in n.accepted.items()
                    if g == gid and t >= started}
        floods = len(versions)
        self.assertLessEqual(floods, corrections + 2)        # the duplicate's start, plus one per correction
        self.assertLessEqual(max(versions.values()), started + 60)   # nothing new after the first minute
        late = [a for a in sim.attempts if a[3] == "push" and a[0] >= started + 600]
        rate = len(late) / (sim.now - started - 600)
        self.assertLess(rate, 1.0)                           # the settled trickle
        self.report(f"duplicate id: {corrections} corrections, {floods} floods, settled within "
                    f"{max(versions.values()) - started:.0f} s; {rate:.2f} pushes/s afterwards", sim)


if __name__ == "__main__":
    unittest.main()
