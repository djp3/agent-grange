"""agent-grange: wires the pieces of the grange package together.

Started by entrypoint.py after the launch-time freshness check (startup step
1), which exec's this file, so it runs as process 1 and handles SIGTERM
itself. Startup, per requirements 6.1:

  2. read configuration; a missing or invalid GRANGE_ID refuses (exit 78)
  4. bind the local API to loopback and the container's bridge address,
     never to all interfaces
  6. serve

Steps 3 and 5 (tailnet wait, discovery and the startup repairs) arrive with
the gossip and sidecar phases.
"""
from __future__ import annotations

import os
import signal
import sys
import threading

import freshness
from grange import freshness_watch
from grange.api_local import LocalApp, make_handler
from grange.clock import SystemClock
from grange.config import EX_CONFIG, Config, ConfigError
from grange.httpbase import Server
from grange.netaddr import local_api_addresses
from grange.state import Store
from grange.stats import Stats
from grange.ticker import Ticker


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def refuse(reason: str) -> None:
    log(f"REFUSING TO START: {reason}")
    sys.exit(EX_CONFIG)


def main() -> None:
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
    app = LocalApp(store, stats, build, fresh_cfg, fresh_state)

    handler = make_handler(app)
    servers = []
    for addr in local_api_addresses():
        try:
            servers.append(Server((addr, cfg.api_port), handler))
        except OSError as e:
            refuse(f"cannot bind the local API to {addr}:{cfg.api_port}: {e}")

    ticker = Ticker(store, clock, log=log)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    for server in servers:
        threading.Thread(target=server.serve_forever, name=f"api-{server.server_address[0]}",
                         daemon=True).start()
    ticker.start()
    log(f"grange {cfg.grange_id!r} incarnation {store.incarnation} serving the local API on "
        + ", ".join(f"{s.server_address[0]}:{s.server_address[1]}" for s in servers)
        + (f"; plain mode, peers {', '.join(cfg.peers)}" if cfg.plain_mode else ""))

    stop.wait()
    log("SIGTERM: shutting down")
    ticker.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
