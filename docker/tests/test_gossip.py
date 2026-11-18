"""The gossip core, the gossip endpoint, and the HTTP runtime (plan phase 3)."""
from __future__ import annotations

import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from grange.api_gossip import GossipApp, make_handler, parse_reply
from grange.clock import FakeClock, SystemClock
from grange.config import Config
from grange.discovery import StaticDiscovery
from grange.gossip import Core, Result, choose_neighbours
from grange.httpbase import Server
from grange.model import Agent, encode_json
from grange.runtime import HttpTransport, Scheduler, startup
from grange.state import Store
from grange.stats import Stats

from sim.harness import Sim


def addrs(n, base="100.64.0"):
    return [f"{base}.{i}:7789" for i in range(1, n + 1)]


class NeighbourChoiceTest(unittest.TestCase):
    def test_successor_plus_hash_ranked(self):
        live = addrs(12)
        own = "100.64.0.5:7789"
        nbrs = choose_neighbours("djp3", own, [a for a in live if a != own], 5)
        self.assertEqual(len(nbrs), 5)
        self.assertEqual(nbrs[0], "100.64.0.6:7789")
        self.assertEqual(choose_neighbours("djp3", own, [a for a in live if a != own], 5), nbrs)   # stable

    def test_successor_wraps_in_byte_order(self):
        live = ["100.64.0.10:7789", "100.64.0.2:7789", "100.64.0.1:7789"]
        # Byte order: "100.64.0.10:7789" < "100.64.0.1:7789" < "100.64.0.2:7789".
        self.assertEqual(choose_neighbours("x", "100.64.0.2:7789", live, 1), ["100.64.0.10:7789"])
        self.assertEqual(choose_neighbours("x", "100.64.0.10:7789", live, 1), ["100.64.0.1:7789"])

    def test_successor_edges_form_one_ring(self):
        live = addrs(30)
        seen, cur = set(), live[0]
        for _ in live:
            seen.add(cur)
            cur = choose_neighbours("g", cur, [a for a in live if a != cur], 5)[0]
        self.assertEqual(seen, set(live))
        self.assertEqual(cur, live[0])

    def test_differs_per_grange(self):
        live = addrs(40)
        a = set(choose_neighbours("alice", "100.64.9.9:7789", live, 5)[1:])
        b = set(choose_neighbours("bob", "100.64.9.9:7789", live, 5)[1:])
        self.assertNotEqual(a, b)

    def test_one_peer_joining_or_leaving_changes_at_most_one_neighbour(self):
        rng = random.Random(7)
        for trial in range(200):
            live = rng.sample(addrs(60), rng.randint(3, 40))
            own = rng.choice(addrs(60, base="100.64.1"))
            before = set(choose_neighbours(f"g{trial}", own, live, 5))
            leaving = rng.choice(live)
            after_leave = set(choose_neighbours(f"g{trial}", own, [a for a in live if a != leaving], 5))
            self.assertLessEqual(len(before - after_leave), 1)
            joining = rng.choice([a for a in addrs(60) if a not in live] or ["100.64.2.1:7789"])
            after_join = set(choose_neighbours(f"g{trial}", own, live + [joining], 5))
            self.assertLessEqual(len(before - after_join), 1)

    def test_fewer_live_than_fanout(self):
        self.assertEqual(sorted(choose_neighbours("g", "100.64.0.9:7789", addrs(3), 5)), sorted(addrs(3)))
        self.assertEqual(choose_neighbours("g", "x:1", [], 5), [])


class SimMeshTest(unittest.TestCase):
    def settled(self, n=6, **kw):
        """Granges started one after another discover the later ones at their
        first refresh, DISCOVERY_SECONDS in."""
        sim = Sim(n, **kw)
        sim.run_for(35)
        for node in sim.nodes.values():
            self.assertEqual(len(node.board()), n, node.gid)
        return sim

    def test_first_record_leaves_by_the_startup_repairs_followup(self):
        sim = Sim(0)
        a = sim.add("alice")
        b = sim.add("bob")                       # bob's startup repair with alice
        sim.run_for(0.2)                         # one round trip and the follow-up push
        self.assertIsNotNone(a.store.body("bob"))
        self.assertIsNotNone(b.store.body("alice"))
        self.assertEqual([k for (_, f, t, k, ok) in sim.attempts if f == b.addr], ["repair", "push"])

    def test_change_converges_after_one_round_of_pushes(self):
        sim = self.settled(6)
        node = list(sim.nodes.values())[2]
        ch = sim.change(node)
        sim.run_for(1)
        self.assertIsNotNone(sim.convergence(ch))
        self.assertLessEqual(sim.convergence(ch), 2 * sim.latency + 1e-9)   # a push and at most one forward

    def test_forward_once_does_not_loop(self):
        sim = self.settled(10)
        pushes_before = sum(n.core.sent["push"] for n in sim.nodes.values())
        ch = sim.change(list(sim.nodes.values())[0])
        sim.run_for(2.5)                         # before the next round of heartbeats matter
        pushes = sum(n.core.sent["push"] for n in sim.nodes.values()) - pushes_before
        self.assertLessEqual(pushes, 10 * 5)     # each grange pushes a new body at most once per neighbour
        self.assertIsNotNone(sim.convergence(ch))

    def test_repair_heals_a_dropped_push(self):
        sim = self.settled(2)
        a, b = sim.nodes.values()
        sim.cut([a], [b])
        ch = sim.change(a)
        sim.run_for(1.5)
        self.assertFalse(b.holds(ch[0], ch[2]))
        self.assertIn(b.addr, a.core.failed)
        sim.heal()
        sim.run_for(6.5)                          # b's next repair with a, or a's after the refresh
        self.assertTrue(b.holds(ch[0], ch[2]))

    def test_repair_fetches_newer_and_pushes_back_what_the_peer_lacks(self):
        sim = self.settled(2)
        a, b = sim.nodes.values()
        sim.cut([a], [b])
        ca, cb = sim.change(a), sim.change(b)
        sim.heal()
        a.core.repair(b.addr)
        sim.run_for(0.5)
        self.assertTrue(a.holds(cb[0], cb[2]))
        self.assertTrue(b.holds(ca[0], ca[2]))

    def test_leaving_propagates(self):
        sim = self.settled(8)
        node = list(sim.nodes.values())[3]
        sim.leave(node)
        sim.run_for(0.5)
        for other in sim.nodes.values():
            if other is not node:
                self.assertNotIn(node.gid, other.board())

    def test_dead_peer_leaves_the_live_set_and_neighbours(self):
        sim = self.settled(8, hint_lag=20)
        dead = list(sim.nodes.values())[4]
        sim.kill(dead)
        sim.run_for(90)                           # past expiry and the hint's lag
        for node in sim.up_nodes():
            self.assertNotIn(dead.addr, node.core.live())
            self.assertNotIn(dead.addr, node.core.neighbours())
            self.assertNotIn(dead.gid, node.board())

    def test_failed_address_re_enters_when_discovery_hints_online(self):
        sim = self.settled(3)
        a = list(sim.nodes.values())[0]
        target = list(sim.nodes.values())[1].addr
        a.core._on_result(target, "push", None, Result(False, 503))
        self.assertNotIn(target, a.core.live())
        a.core.refresh_discovery()
        self.assertIn(target, a.core.live())

    def test_409_or_503_marks_failed_and_an_accepted_request_clears_it(self):
        sim = self.settled(3)
        a, b, _ = sim.nodes.values()
        for status in (409, 503):
            a.core._on_result(b.addr, "push", None, Result(False, status))
            self.assertIn(b.addr, a.core.failed)
            a.core.on_incoming(b.addr, b.gid)
            self.assertNotIn(b.addr, a.core.failed)

    def test_a_changed_from_rebinds_the_address(self):
        sim = self.settled(2)
        a, b = sim.nodes.values()
        self.assertEqual(a.core.binding[b.addr], b.gid)
        b.gid = "robert"
        sim.restart(b)                            # a new grange id at the same address
        sim.run_for(4)
        self.assertEqual(a.core.binding[b.addr], "robert")
        self.assertTrue(any("is now grange 'robert'" in m for _, m in a.logs))

    def test_halves_reconnect_through_the_hint(self):
        sim = self.settled(8)
        nodes = list(sim.nodes.values())
        left, right = nodes[:4], nodes[4:]
        sim.cut(left, right)
        sim.run_for(80)
        self.assertEqual(len(nodes[0].board()), 4)
        sim.heal()
        sim.run_for(45)                           # DISCOVERY_SECONDS plus 15
        for n in nodes:
            self.assertEqual(len(n.board()), 8, n.gid)

    def test_halves_reconnect_through_the_probe_with_hints_withheld(self):
        sim = self.settled(8)
        nodes = list(sim.nodes.values())
        left, right = nodes[:4], nodes[4:]
        sim.cut(left, right)
        sim.withhold_hints = True                 # one side lost headscale: every hint offline
        sim.run_for(80)
        sim.heal()
        sim.run_for(45)
        for n in nodes:
            self.assertEqual(len(n.board()), 8, n.gid)

    def test_resume_readmits_a_paused_grange_within_one_repair(self):
        sim = self.settled(6)
        node = list(sim.nodes.values())[2]
        sim.pause(node)
        sim.run_for(75)
        for other in sim.up_nodes():
            self.assertNotIn(node.gid, other.board())
        sim.unpause(node)
        sim.run_for(1.5)                          # ticker sees the gap, bumps, repairs; follow-up readmits
        self.assertEqual(node.ticker.resumes, 1)
        for other in sim.up_nodes():
            self.assertIn(node.gid, other.board(), other.gid)

    def test_nothing_is_sent_while_stale(self):
        sim = self.settled(3)
        a = list(sim.nodes.values())[0]
        a.core.set_stale(True)
        before = len([x for x in sim.attempts if x[1] == a.addr])
        sim.change(a)
        sim.run_for(40)
        self.assertEqual(len([x for x in sim.attempts if x[1] == a.addr]), before)


class GossipEndpointTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cfg = Config(grange_id="alice")
        self.store = Store(self.cfg, self.clock, log=lambda m: None)
        self.stats = Stats(self.clock)
        self.logs = []
        self.app = GossipApp(self.store, None, self.stats, self.cfg, allow_source=lambda ip: ip.startswith("100."),
                             log=self.logs.append)
        self.peer = Store(Config(grange_id="bob"), self.clock, log=lambda m: None)

    def raw(self, msg_json):
        return encode_json(msg_json)

    def test_push_is_204_and_repair_is_200(self):
        status, body = self.app.handle("100.64.0.2", self.raw(self.peer.push_message([self.peer.own_body()]).to_json()))
        self.assertEqual((status, body), (204, None))
        status, body = self.app.handle("100.64.0.2", self.raw(self.peer.repair_request().to_json()))
        self.assertEqual(status, 200)
        reply = parse_reply(encode_json(body), self.peer.cfg)
        self.assertEqual(set(reply.digest), {"alice", "bob"})

    def test_group_mismatch_is_409_counted_and_logged_once(self):
        other = Store(Config(grange_id="eve", group="proto-work"), self.clock, log=lambda m: None)
        for _ in range(3):
            status, _ = self.app.handle("100.64.0.9", self.raw(other.repair_request().to_json()))
            self.assertEqual(status, 409)
        self.assertEqual(self.stats.rejected["group"], 3)
        self.assertEqual(len([m for m in self.logs if "group" in m]), 1)

    def test_proto_mismatch_is_409(self):
        msg = self.peer.repair_request().to_json()
        msg["proto"] = 2
        self.assertEqual(self.app.handle("100.64.0.2", self.raw(msg))[0], 409)
        self.assertEqual(self.stats.rejected["proto"], 1)

    def test_source_outside_the_allowed_set_is_403(self):
        self.assertEqual(self.app.handle("192.168.1.5", self.raw(self.peer.repair_request().to_json()))[0], 403)
        self.assertEqual(self.stats.rejected["identity"], 1)

    def test_identity_check_failure_is_403(self):
        self.app.identity = lambda ip: False
        self.assertEqual(self.app.handle("100.64.0.2", self.raw(self.peer.repair_request().to_json()))[0], 403)
        self.assertEqual(self.stats.rejected["identity"], 1)

    def test_malformed_is_400_and_over_caps_413(self):
        self.assertEqual(self.app.handle("100.64.0.2", b"{")[0], 400)
        self.assertEqual(self.app.handle("100.64.0.2", self.raw({"proto": 1, "group": "default"}))[0], 400)
        big = {"proto": 1, "group": "default", "from": "bob", "records": [self.peer.own_body().to_json()] * 201}
        self.assertEqual(self.app.handle("100.64.0.2", self.raw(big))[0], 413)
        self.assertEqual(self.app.handle("100.64.0.2", b" " * ((1 << 20) + 1))[0], 413)
        self.assertEqual(self.stats.rejected["overflow"], 2)

    def test_repair_with_records_is_400(self):
        msg = self.peer.repair_request().to_json()
        msg["records"] = [self.peer.own_body().to_json()]
        self.assertEqual(self.app.handle("100.64.0.2", self.raw(msg))[0], 400)


class HttpRuntimeTest(unittest.TestCase):
    """Three granges over real sockets on loopback (R6.11)."""

    def setUp(self):
        self.clock = SystemClock()
        self.granges = []
        servers = [Server(("127.0.0.1", 0), None) for _ in range(3)]
        ports = [s.server_address[1] for s in servers]
        for s in servers:
            s.server_close()
        self.addrs = [f"127.0.0.1:{p}" for p in ports]
        for i, addr in enumerate(self.addrs):
            cfg = Config(grange_id=f"g{i}", peers=tuple(a for a in self.addrs if a != addr),
                         repair_s=0.5, heartbeat_s=0.5, discovery_s=5)
            store = Store(cfg, self.clock, log=lambda m: None)
            pool = ThreadPoolExecutor(max_workers=2 * cfg.fanout)
            core = Core(store, cfg, self.clock, HttpTransport(cfg, pool), StaticDiscovery(cfg.peers), addr,
                        stats=Stats(self.clock))
            received = []
            app = GossipApp(store, core, core.stats, cfg, allow_source=lambda ip: True)
            orig = app.handle
            app.handle = lambda ip, raw, orig=orig, received=received: (received.append(time.monotonic()), orig(ip, raw))[1]
            server = Server(("127.0.0.1", int(addr.rsplit(":", 1)[1])), make_handler(app))
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            self.granges.append((store, core, server, received, pool))
        for store, core, *_ in self.granges:
            startup(core, lambda m: None)

    def tearDown(self):
        for store, core, server, received, pool in self.granges:
            server.shutdown()
            server.server_close()
            pool.shutdown(wait=False)

    def test_local_change_reaches_every_neighbour_within_one_second(self):
        store0 = self.granges[0][0]
        for _, _, _, received, _ in self.granges:
            received.clear()
        changed = time.monotonic()
        store0.set_agents(store0.agents + (Agent("g0", "new session", "busy"),))
        deadline = changed + 1.0
        while time.monotonic() < deadline:
            if all(s.body("g0") is not None and s.body("g0").rev == store0.rev for s, *_ in self.granges[1:]):
                break
            time.sleep(0.01)
        for store, _, _, received, _ in self.granges[1:]:
            self.assertEqual(store.body("g0").rev, store0.rev)
            self.assertTrue(received and received[0] - changed < 1.0)

    def test_scheduler_runs_repairs_and_heartbeats(self):
        sched = [Scheduler(core, self.clock, lambda m: None).start() for _, core, *_ in self.granges]
        try:
            time.sleep(1.6)
            s0 = self.granges[0][0]
            counter = s0.counter
            self.assertGreater(counter, 2)                            # heartbeats ran
            time.sleep(1.2)
            seen = self.granges[1][0].body("g0").counter
            self.assertGreaterEqual(seen, counter)                    # carried by repairs
        finally:
            for s in sched:
                s.stop()

    def test_unreachable_peer_fails_and_is_marked(self):
        store, core, *_ = self.granges[0]
        dead = "127.0.0.1:9"                                          # discard port: refused at once
        done = threading.Event()
        result = {}
        core._send(dead, store.repair_request(), "repair", on_done=lambda r: (result.update(r=r), done.set()))
        self.assertTrue(done.wait(5))
        self.assertFalse(result["r"].ok)
        self.assertIn(dead, core.failed)


if __name__ == "__main__":
    unittest.main()
