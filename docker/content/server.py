"""agent-grange: wires the pieces of the grange package together.

Started by entrypoint.py after the launch-time freshness check (startup step
1), which exec's this file, so it runs as process 1 and handles SIGTERM
itself. Startup, per requirements 6.1:

  2. read configuration; a missing or invalid GRANGE_ID refuses (exit 78)
  3. tailnet mode only: wait for the sidecar's tailnet address (plan phase 5)
  4. bind the local API to loopback and the container's bridge address, and
     the gossip listener to its one address; never to all interfaces
  5. discovery once, then repairs with up to FANOUT peers, at most 5 s
  6. serve

Plain mode (GRANGE_PEERS set, R6.9) is for testing only: no tailnet, the
gossip listener on the bridge address, sources limited to that network.
With neither GRANGE_PEERS nor a tailnet (until plan phase 5.2) the grange is
alone: local API only.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import freshness
from grange import api_gossip, freshness_watch, runtime
from grange.api_local import LocalApp, make_handler
from grange.clock import SystemClock
from grange.config import EX_CONFIG, Config, ConfigError
from grange.discovery import NoDiscovery, StaticDiscovery
from grange.gossip import Core
from grange.httpbase import Server
from grange.netaddr import default_route_address, in_networks, local_api_addresses, local_networks
from grange.state import Store
from grange.stats import Stats
from grange.ticker import Ticker


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def refuse(reason: str) -> None:
    log(f"REFUSING TO START: {reason}")
    sys.exit(EX_CONFIG)


def bind(addr: str, port: int, handler, what: str) -> Server:
    try:
        return Server((addr, port), handler)
    except OSError as e:
        refuse(f"cannot bind the {what} to {addr}:{port}: {e}")


def serve(server: Server, name: str) -> None:
    threading.Thread(target=server.serve_forever, name=name, daemon=True).start()


def main() -> None:
    # 2. configuration
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        refuse(str(e))

    fresh_cfg = freshness.Config.from_env()
    build = {"repo": fresh_cfg.repo, "branch": os.environ.get("GIT_BRANCH", "unknown"),
             "commit": fresh_cfg.built, "dirty": fresh_cfg.dirty}
    fresh_state = freshness_watch.start(fresh_cfg, log)

    clock = SystemClock()
    store = Store(cfg, clock, log)
    stats = Stats(clock)

    # 3. and 4.: where gossip listens, who may talk to it, how peers are found
    if cfg.plain_mode:
        gossip_addr = default_route_address()
        if gossip_addr is None:
            refuse("plain mode needs a bridge address for the gossip listener; none found")
        own_address = f"{socket.gethostname()}:{cfg.gossip_port}"
        networks = local_networks()
        allow = lambda ip: in_networks(ip, networks)      # noqa: E731
        discovery = StaticDiscovery(cfg.peers)
        resolve = runtime.Resolver()
    else:
        gossip_addr = None                       # tailnet mode arrives in plan phase 5.2
        own_address = f"lone:{cfg.gossip_port}"
        allow = lambda ip: False                 # noqa: E731
        discovery = NoDiscovery()
        resolve = None

    pool = ThreadPoolExecutor(max_workers=2 * cfg.fanout, thread_name_prefix="gossip")
    core = Core(store, cfg, clock, runtime.HttpTransport(cfg, pool), discovery, own_address,
                stats=stats, resolve=resolve, log=log)
    fresh_state.subscribe(core.set_stale)       # R5.16: a stale image stops sending
    if fresh_state.is_stale():
        core.set_stale(True)

    local_servers = [bind(a, cfg.api_port, make_handler(LocalApp(store, stats, build, fresh_cfg, fresh_state)),
                          "local API") for a in local_api_addresses()]
    gossip_server = None
    if gossip_addr is not None:
        app = api_gossip.GossipApp(store, core, stats, cfg, allow, log=log)
        gossip_server = bind(gossip_addr, cfg.gossip_port,
                             api_gossip.make_handler(app, fresh_cfg, fresh_state), "gossip listener")
        serve(gossip_server, "gossip")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # 5. discovery and the startup repairs
    runtime.startup(core, log)

    # 6. serve
    for server in local_servers:
        serve(server, f"api-{server.server_address[0]}")
    ticker = Ticker(store, clock, on_resume=core.on_resume, log=log).start()
    scheduler = runtime.Scheduler(core, clock, log).start()
    where = ", ".join(f"{s.server_address[0]}:{s.server_address[1]}" for s in local_servers)
    mode = (f"plain mode as {own_address}, gossip on {gossip_addr}:{cfg.gossip_port}, "
            f"peers {', '.join(cfg.peers)}" if cfg.plain_mode else "alone (no GRANGE_PEERS, no tailnet yet)")
    log(f"grange {cfg.grange_id!r} incarnation {store.incarnation} serving the local API on {where}; {mode}")

    stop.wait()
    log("SIGTERM: leaving the mesh")
    scheduler.stop()
    ticker.stop()
    runtime.shutdown(core, log)
    sys.stderr.flush()
    os._exit(0)                                  # do not wait on a worker still inside an exchange


if __name__ == "__main__":
    main()
