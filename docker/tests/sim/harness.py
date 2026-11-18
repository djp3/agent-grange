"""A discrete-event mesh of granges for the simulation (R10.5) and the gossip
unit tests.

Every grange is a Store, the gossip core, the gossip request handler, and the
ticker, all used unchanged. Messages are built by the Store, serialized to
JSON, handled by `GossipApp.handle` exactly as the HTTP handler does, and the
replies parsed by `parse_reply` exactly as the HTTP transport does. Around
them: a fake clock, a fake transport with a per-hop latency that can cut
links and counts bytes, and a fake discovery whose online hint follows
whether a grange is up, with a configurable lag.

Failure timing follows the runtime: an unreachable address fails after
CONNECT_TIMEOUT_SECONDS; a paused process accepts the connection and never
answers, so it fails after EXCHANGE_TIMEOUT_SECONDS.
"""
from __future__ import annotations

import heapq
import itertools
import random
from collections import defaultdict
from typing import Optional

from grange.api_gossip import GossipApp, parse_reply
from grange.config import Config
from grange.gossip import Core, Result
from grange.model import Agent, encode_json
from grange.state import FORWARD, LOCAL, Store
from grange.ticker import Ticker

WALL0 = 1_763_974_800.0
HTTP_OVERHEAD = 150                  # bytes per request and per response, headers included in the count

UP, DOWN, PAUSED = "up", "down", "paused"


class SimClock:
    def __init__(self, sim, wall_offset: float = 0.0):
        self.sim = sim
        self.wall_offset = wall_offset

    def monotonic(self) -> float:
        return self.sim.now

    def wall(self) -> float:
        return WALL0 + self.sim.now + self.wall_offset


class SimDiscovery:
    def __init__(self, sim, node):
        self.sim, self.node = sim, node

    @property
    def hostnames(self) -> dict:
        return self.sim.hostnames

    def peers(self) -> dict:
        withheld = self.node.hints_withheld
        return {a: (False if withheld else self.sim.hint(a)) for a in self.sim.addresses() if a != self.node.addr}


class SimTransport:
    def __init__(self, sim, node):
        self.sim, self.node = sim, node

    def send(self, addr, msg, expect_reply, done) -> None:
        self.sim.deliver(self.node, addr, msg, expect_reply, done)


class Node:
    def __init__(self, sim, addr: str, gid: str, agents: int, wall_offset: float = 0.0):
        self.sim = sim
        self.addr = addr
        self.ip = addr.rpartition(":")[0]
        self.gid = gid
        self.state = UP
        self.gen = 0
        self.history = [(sim.now, True)]          # (time, up) for the hint
        self.expired: list = []                   # (time, gid) this node expired
        self.accepted: dict = {}                  # (gid, incarnation, rev) -> first time held
        self.n_agents = agents
        self.farm_alive = True
        self.hints_withheld = False               # this grange lost headscale: every hint offline
        self.build(wall_offset)

    def build(self, wall_offset: float = 0.0) -> None:
        sim = self.sim
        self.gen += 1
        self.clock = SimClock(sim, wall_offset)
        self.cfg = sim.cfg.with_(grange_id=self.gid)
        self.store = Store(self.cfg, self.clock, log=self.logline)
        self.core = Core(self.store, self.cfg, self.clock, SimTransport(sim, self), SimDiscovery(sim, self),
                         self.addr, rng=random.Random(sim.rng.random()), log=self.logline)
        self.app = GossipApp(self.store, self.core, None, self.cfg, allow_source=lambda ip: True,
                             log=self.logline)
        self.ticker = Ticker(self.store, self.clock, on_resume=self.core.on_resume, log=self.logline,
                             on_expire=lambda gone: self.expired.extend((sim.now, g) for g in gone))
        self.store.subscribe(self._on_event)
        self.logs: list = []
        self.agents = [Agent(self.gid, f"{self.gid}-pr-{k} task", "busy") for k in range(self.n_agents)]

    def logline(self, msg: str) -> None:
        self.logs.append((self.sim.now, msg))

    def _on_event(self, ev) -> None:
        r = ev.record
        self.accepted.setdefault((r.grange, r.incarnation, r.rev), self.sim.now)

    def holds(self, gid: str, rev: int, incarnation: Optional[int] = None) -> bool:
        rec = self.store.body(gid)
        return rec is not None and rec.rev >= rev and (incarnation is None or rec.incarnation == incarnation)

    def board(self) -> set:
        return {g["grange"] for g in self.store.picture()["granges"]}


class Sim:
    def __init__(self, n: int = 0, latency: float = 0.05, seed: int = 1, hint_lag: float = 30.0,
                 agents: int = 10, **cfg):
        self.now = 0.0
        self.latency = latency
        self.hint_lag = hint_lag
        self.rng = random.Random(seed)
        self.cfg = Config(grange_id="sim", **cfg)
        self.agents_per = agents
        self.nodes: dict = {}                     # addr -> Node
        self._queue: list = []
        self._seq = itertools.count()
        self.cuts: list = []                      # [(set_a, set_b)]
        self.withhold_hints = False
        self.hostnames: dict = {}                 # address -> sidecar hostname, a hint only (R6.19)
        self.bytes = defaultdict(lambda: defaultdict(int))   # addr -> minute -> bytes
        self.attempts: list = []                  # (t, from addr, to addr, kind, ok) as each finishes
        for i in range(n):
            self.add()

    # ------------------------------------------------------------------ nodes
    def addresses(self) -> list:
        return list(self.nodes)

    def add(self, gid: Optional[str] = None, addr: Optional[str] = None, start: bool = True,
            wall_offset: float = 0.0) -> Node:
        i = len(self.nodes) + 1
        addr = addr or f"100.64.{i // 250}.{i % 250 + 1}:7789"
        gid = gid or f"g{i:03}"
        node = Node(self, addr, gid, self.agents_per, wall_offset)
        self.nodes[addr] = node
        if start:
            self.start(node)
        return node

    def start(self, node: Node) -> None:
        node.state = UP
        node.store.set_agents(node.agents)
        node.core.start()
        self._schedule_node(node)

    def _schedule_node(self, node: Node) -> None:
        gen = node.gen

        def core_tick():
            if node.gen != gen or node.state != UP:
                return
            deadline = node.core.tick()
            self.at(max(deadline, self.now + 0.001), core_tick)

        def ticker_tick():
            if node.gen != gen or node.state != UP:
                return
            node.ticker.run_once()
            self.at(self.now + 1.0, ticker_tick)

        def farm_tick():
            if node.gen != gen or node.state != UP:
                return
            if node.farm_alive:
                node.store.set_agents(node.agents)      # farm's heartbeat push (R5.2)
            self.at(self.now + 30.0, farm_tick)

        self.at(self.now, core_tick)
        self.at(self.now + 1.0, ticker_tick)
        self.at(self.now + 30.0 * self.rng.random(), farm_tick)

    def _set_state(self, node: Node, state: str) -> None:
        was_up = node.state == UP
        node.state = state
        if (state == UP) != was_up:
            node.history.append((self.now, state == UP))

    def kill(self, node: Node) -> None:
        """The process dies without a leaving body; nothing answers."""
        self._set_state(node, DOWN)
        node.gen += 1

    def pause(self, node: Node) -> None:
        """Frozen with its clock running on: docker pause, or a sleep whose
        stopwatch runs. The machine is away, so the hint turns offline too."""
        self._set_state(node, PAUSED)

    def unpause(self, node: Node) -> None:
        self._set_state(node, UP)
        node.gen += 1
        self._schedule_node(node)                 # the ticker sees the gap

    def restart(self, node: Node, wall_offset: float = 0.0) -> None:
        """A fresh process at the same address: a new incarnation."""
        self._set_state(node, UP)
        node.build(wall_offset)
        self.start(node)

    def leave(self, node: Node) -> None:
        node.core.leave()
        self.at(self.now + 2.0, lambda: (self._set_state(node, DOWN), setattr(node, "gen", node.gen + 1)))

    def hint(self, addr: str) -> bool:
        if self.withhold_hints:
            return False
        node = self.nodes[addr]
        t = self.now - self.hint_lag
        up = node.history[0][1]
        for when, state in node.history:
            if when <= t:
                up = state
        return up

    def cut(self, a, b) -> None:
        self.cuts.append(({n.addr for n in a}, {n.addr for n in b}))

    def heal(self) -> None:
        self.cuts = []

    def is_cut(self, x: str, y: str) -> bool:
        return any((x in a and y in b) or (x in b and y in a) for a, b in self.cuts)

    # -------------------------------------------------------------- transport
    def count(self, node: Node, n: int) -> None:
        self.bytes[node.addr][int(self.now // 60)] += n + HTTP_OVERHEAD

    def deliver(self, sender: Node, addr: str, msg, expect_reply: bool, done) -> None:
        payload = encode_json(msg.to_json())
        kind = "repair" if expect_reply else "push"
        inner = done

        def done(result, _t0=self.now):
            self.attempts.append((_t0, sender.addr, addr, kind, result.ok))
            inner(result)
        self.count(sender, len(payload))
        target = self.nodes.get(addr)
        sgen = sender.gen
        if target is None or target.state == DOWN or self.is_cut(sender.addr, addr):
            self.at(self.now + self.cfg.connect_timeout_s, lambda: self._finish(sender, sgen, done, Result(False)))
            return
        if target.state == PAUSED:
            self.at(self.now + self.cfg.exchange_timeout_s, lambda: self._finish(sender, sgen, done, Result(False)))
            return

        def arrive():
            if target.state != UP:
                self.at(self.now + self.cfg.exchange_timeout_s - self.latency,
                        lambda: self._finish(sender, sgen, done, Result(False)))
                return
            self.count(target, len(payload))
            status, body = target.app.handle(sender.ip, payload)
            reply = encode_json(body) if body is not None else b""
            self.count(target, len(reply))

            def back():
                self.count(sender, len(reply)) if sender.gen == sgen else None
                ok = 200 <= status < 300
                resp = None
                if ok and expect_reply:
                    resp = parse_reply(reply, sender.cfg)
                    ok = resp is not None
                self._finish(sender, sgen, done, Result(ok, status, resp))
            self.at(self.now + self.latency, back)
        self.at(self.now + self.latency, arrive)

    def _finish(self, sender: Node, gen: int, done, result: Result) -> None:
        if sender.gen == gen and sender.state == UP:
            done(result)

    # ------------------------------------------------------------------- time
    def at(self, t: float, fn) -> None:
        heapq.heappush(self._queue, (t, next(self._seq), fn))

    def run_until(self, t_end: float) -> None:
        while self._queue and self._queue[0][0] <= t_end:
            t, _, fn = heapq.heappop(self._queue)
            self.now = max(self.now, t)
            fn()
        self.now = max(self.now, t_end)

    def run_for(self, seconds: float) -> None:
        self.run_until(self.now + seconds)

    # -------------------------------------------------------------- measures
    def up_nodes(self) -> list:
        return [n for n in self.nodes.values() if n.state == UP]

    def change(self, node: Node) -> tuple:
        """Flip one agent's state on `node`. Returns (gid, incarnation, rev, t0)."""
        k = self.rng.randrange(len(node.agents))
        a = node.agents[k]
        node.agents[k] = Agent(a.owner, a.name, "waiting" if a.state == "busy" else "busy")
        node.store.set_agents(node.agents)
        return node.gid, node.store.incarnation, node.store.rev, self.now

    def convergence(self, change: tuple, nodes=None) -> Optional[float]:
        """Seconds until every node in `nodes` held the change, or None."""
        gid, inc, rev, t0 = change
        worst = 0.0
        for n in (nodes if nodes is not None else self.up_nodes()):
            if n.gid == gid:
                continue
            t = n.accepted.get((gid, inc, rev))
            if t is None:
                if not n.holds(gid, rev, inc):
                    return None
                continue                         # held by a later rev already
            worst = max(worst, t - t0)
        return worst

    def max_rate(self, nodes=None, minutes=None) -> float:
        """Highest bytes per second, sent plus received, of any node over any
        whole minute in `minutes`."""
        worst = 0.0
        for n in (nodes if nodes is not None else self.nodes.values()):
            per_min = self.bytes[n.addr]
            for m in (minutes if minutes is not None else per_min):
                worst = max(worst, per_min.get(m, 0) / 60.0)
        return worst

    def false_expiries(self, alive) -> list:
        alive_ids = {n.gid for n in alive}
        return [(n.gid, t, g) for n in alive for (t, g) in n.expired if g in alive_ids]
