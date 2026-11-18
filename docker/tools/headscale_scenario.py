#!/usr/bin/env python3
"""Drives the local headscale stack (R10.3, plan phase 6) and times it.

Brings up compose.headscale.yml (headscale, three sidecar and grange pairs,
and a fourth sidecar joined without the tag), runs a fake farm against each
grange, then the checks below, prints a table, tears down, and exits
non-zero on any failure. Host-side, Python 3.9 or later, standard library
only (R8.8). Run it with `make test-headscale`.

  check                              pass condition
  change over the tailnet            visible on all three boards within 5 s
  untagged device, ACL in place      cannot connect to any grange's gossip port
  gossip port binding                answers a grange on the tailnet address; refuses
                                     the bridge address from another container; not
                                     reachable from the host
  untagged device, ACL opened        connects, and the whois check alone refuses it with
                                     403, counted in rejected.identity (R5.13)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_farm import FakeFarm  # noqa: E402
from scenario import http_json, run, say, wait_for  # noqa: E402

HERE = Path(__file__).resolve().parent.parent           # docker/
PROJECT = "agent-grange-headscale"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", str(HERE / "compose.headscale.yml")]
PORTS = {"a": 17981, "b": 17982, "c": 17983}
FARMS: dict = {}
RESULTS: list = []

# Run inside a sidecar's namespace: one gossip repair request to target:7789.
GOSSIP_PROBE = (
    "import json,sys,urllib.request,urllib.error\n"
    "b=json.dumps({'proto':1,'group':'default','from':'probe','digest':{},'records':[]}).encode()\n"
    "r=urllib.request.Request('http://'+sys.argv[1]+':7789/v1/gossip',data=b,method='POST')\n"
    "try:\n  print(urllib.request.urlopen(r,timeout=4).status)\n"
    "except urllib.error.HTTPError as e:\n  print(e.code)\n"
    "except OSError as e:\n  print('unreachable', type(e).__name__)\n")


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    say(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def compose(*args, env=None, timeout=300):
    return run(COMPOSE + list(args), env=env, timeout=timeout)


def container(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def board(g: str):
    status, body = http_json(f"http://127.0.0.1:{PORTS[g]}/v1/agents", timeout=2)
    return {x["grange"]: x for x in body["granges"]} if status == 200 and body else None


def health(g: str) -> dict:
    return http_json(f"http://127.0.0.1:{PORTS[g]}/health", timeout=2)[1] or {}


def tailnet_ip(sidecar: str) -> str:
    out = run(["docker", "exec", container(sidecar), "tailscale", "--socket",
               "/var/run/tailscale/tailscaled.sock", "ip", "-4"]).stdout.strip()
    return out.splitlines()[0]


def bridge_ip(sidecar: str) -> str:
    return run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container(sidecar)]).stdout.strip()


def probe_from(netns: str, target: str) -> str:
    """Gossip request from inside `netns` (a container name, or a network)."""
    network = f"container:{container(netns)}" if netns.startswith("ts-") else netns
    res = run(["docker", "run", "--rm", "--network", network, "--entrypoint", "python3",
               "agent-grange:local", "-c", GOSSIP_PROBE, target], check=False, timeout=60)
    return (res.stdout.strip() or res.stderr.strip()[-120:])


def up(policy: str = "") -> None:
    env = {"HEADSCALE_POLICY": policy} if policy else {}
    say(f"bringing up {PROJECT}{' with ' + policy if policy else ''}")
    compose("up", "-d", "--wait", env=env, timeout=300)


def visible_everywhere(g: str, state: str) -> bool:
    for other in PORTS:
        if other == g:
            continue
        b = board(other)
        if not b or g not in b or not any(a["state"] == state for a in b[g]["agents"]):
            return False
    return True


def main() -> int:
    logs = ""
    try:
        compose("down", "-v", "-t", "3", timeout=120)
        up()
        for g, port in PORTS.items():
            FARMS[g] = FakeFarm(f"http://127.0.0.1:{port}", owner=g).start()
        t = wait_for(lambda: all((board(g) or {}).keys() >= set(PORTS) for g in PORTS), 90)
        if t is None:
            raise RuntimeError("the three granges never found each other by tag")
        say(f"all three boards full after {t:.1f}s")
        disc = {g: health(g).get("peers_discovered") for g in PORTS}
        record("tag discovery", all(v == 2 for v in disc.values()),
               f"peers_discovered {disc} (the untagged device is not among them)")

        # change over the tailnet
        FARMS["a"].set([("a-pr-1 task", "waiting")])
        t = wait_for(lambda: visible_everywhere("a", "waiting"), 15, step=0.05)
        record("change over the tailnet", t is not None and t <= 5,
               f"{'never' if t is None else f'{t:.2f}s'} (bound 5s)")

        ips = {g: tailnet_ip(f"ts-{g}") for g in PORTS}

        # untagged device with the ACL in place
        blocked = {g: probe_from("ts-x", ip) for g, ip in ips.items()}
        record("untagged device, ACL in place", all(v.startswith("unreachable") for v in blocked.values()),
               f"from laptop-two: {blocked}")

        # binding: tailnet address yes, bridge address no, host no
        from_b = probe_from("ts-b", ips["a"])
        net = f"{PROJECT}_tailnet-lab"
        from_bridge = probe_from(net, bridge_ip("ts-a"))
        host = http_json("http://127.0.0.1:7789/v1/gossip", "POST", {}, timeout=2)[0]
        ok = from_b == "200" and from_bridge.startswith("unreachable") and host == 0
        record("gossip port binding", ok,
               f"grange-b to a's tailnet address: {from_b}; bridge address from another container: "
               f"{from_bridge}; host loopback: {'no answer' if host == 0 else host}")

        # untagged device with the ACL opened: the whois check alone must refuse it
        before = health("a").get("rejected", {}).get("identity", 0)
        up(policy="policy-open.hujson")
        got = {}

        def reached():
            got["a"] = probe_from("ts-x", ips["a"])
            return got["a"] == "403"
        t = wait_for(reached, 90, step=2)
        after = health("a").get("rejected", {}).get("identity", 0)
        record("untagged device, ACL opened", t is not None and after > before,
               f"laptop-two to a: {got.get('a')}; rejected.identity {before} -> {after}")
    except Exception as e:
        record("harness", False, f"{type(e).__name__}: {e}")
    finally:
        for farm in FARMS.values():
            farm.stop()
        logs = run(COMPOSE + ["logs", "--no-color", "--tail", "30"], check=False).stdout
        if not os.environ.get("KEEP_STACK"):
            compose("down", "-v", "-t", "3", timeout=120)
    width = max(len(n) for n, _, _ in RESULTS)
    print("\n" + "-" * 100)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    print("-" * 100)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("\nlast log lines:\n" + logs[-6000:])
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
