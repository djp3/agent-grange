#!/usr/bin/env python3
"""Drives the plain-network harness (R10.2, plan phase 4) and times it.

Brings up compose.plain.yml (five granges, grange-a to grange-e, and a mock
GitHub), runs a fake farm against each, then the scenarios below, prints a
table, tears the stack down, and exits non-zero on any failure. Timings are
the defaults, so a full run takes several minutes. Host-side, Python 3.9 or
later, standard library only (R8.8). Run it with `make test-plain`.

  scenario                         pass condition
  change on one grange             visible on all five boards within 5 s
  fake farm goes silent            farm_stale within FARM_STALE_SECONDS + 5 s, agents kept
  kill one grange                  gone from all boards within EXPIRE_SECONDS + 15 s
  kill, serving again within 20 s  new incarnation on all boards within 5 s of serving,
                                   never absent from a board in between
  clean stop                       gone from all boards within 3 s
  pause past EXPIRE_SECONDS        removed, then readmitted within 5 s of unpause
  stale image                      gossip answers 503 and the grange ages out
  change with FANOUT=2             visible on all five boards within 5 s
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_farm import FakeFarm  # noqa: E402

HERE = Path(__file__).resolve().parent.parent           # docker/
PROJECT = "agent-grange-plain"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", str(HERE / "compose.plain.yml")]
PORTS = {"grange-a": 17881, "grange-b": 17882, "grange-c": 17883, "grange-d": 17884, "grange-e": 17885}
MOCK = "http://127.0.0.1:17890"
EXPIRE_SECONDS = 60
FARM_STALE_SECONDS = 60
FARMS: dict = {}
RESULTS: list = []


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, env=None, check=True, timeout=300):
    full_env = dict(os.environ, **(env or {}))
    res = subprocess.run(cmd, env=full_env, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({res.returncode}): {res.stderr.strip()[-800:]}")
    return res


def container(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def image_commit() -> str:
    out = run(["docker", "image", "inspect", "agent-grange:local", "--format",
               "{{range .Config.Env}}{{println .}}{{end}}"]).stdout
    for line in out.splitlines():
        if line.startswith("GIT_COMMIT="):
            return line.split("=", 1)[1]
    raise RuntimeError("agent-grange:local has no GIT_COMMIT; run `make build`")


def http_json(url: str, method: str = "GET", body=None, timeout: float = 3):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return 0, None


def board(service: str):
    """grange id -> board entry, or None if the grange is not answering."""
    status, body = http_json(f"http://127.0.0.1:{PORTS[service]}/v1/agents", timeout=2)
    if status != 200 or body is None:
        return None
    return {g["grange"]: g for g in body["granges"]}


def wait_for(predicate, timeout: float, step: float = 0.2):
    """Seconds until predicate() is true, or None after `timeout`."""
    start = time.monotonic()
    while True:
        if predicate():
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            return None
        time.sleep(step)


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    say(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def others(service: str):
    return [s for s in PORTS if s != service]


def visible_everywhere(service: str, state: str, among=None) -> bool:
    for s in (among or others(service)):
        b = board(s)
        if b is None or service not in b:
            return False
        if not any(a["state"] == state for a in b[service]["agents"]):
            return False
    return True


def gone_everywhere(service: str) -> bool:
    for s in others(service):
        b = board(s)
        if b is None or service in b:
            return False
    return True


# ------------------------------------------------------------------- the stack

def up(fanout: str = "") -> None:
    env = {"PLAIN_COMMIT": image_commit(), "PLAIN_FANOUT": fanout}
    say(f"bringing up {PROJECT}{' with FANOUT=' + fanout if fanout else ''}")
    run(COMPOSE + ["up", "-d", "--wait", "--force-recreate"], env=env, timeout=240)
    for s, port in PORTS.items():
        if s not in FARMS:
            FARMS[s] = FakeFarm(f"http://127.0.0.1:{port}", owner=s).start()
        else:
            FARMS[s].resume()
    t = wait_for(lambda: all((board(s) or {}).keys() >= set(PORTS) for s in PORTS), 90)
    if t is None:
        raise RuntimeError("the five granges never saw each other")
    say(f"all five boards full after {t:.1f}s")


def down() -> None:
    for farm in FARMS.values():
        farm.stop()
    run(COMPOSE + ["down", "-t", "3"], check=False, timeout=120)


def docker(*args) -> None:
    run(["docker", *args], timeout=60)


def serving(service: str) -> bool:
    return http_json(f"http://127.0.0.1:{PORTS[service]}/health", timeout=1)[0] == 200


# ------------------------------------------------------------------- scenarios

def change_scenario(name: str, service: str = "grange-a") -> None:
    farm = FARMS[service]
    new = "waiting" if farm.agents[0][1] != "waiting" else "busy"
    farm.set([(farm.agents[0][0], new)])
    t = wait_for(lambda: visible_everywhere(service, new), 15, step=0.05)
    record(name, t is not None and t <= 5, f"{'never' if t is None else f'{t:.2f}s'} (bound 5s)")


def farm_silent_scenario() -> None:
    service = "grange-c"
    FARMS[service].silence()
    bound = FARM_STALE_SECONDS + 5

    def flagged():
        b = board("grange-a")
        return bool(b and service in b and b[service]["farm_stale"] and b[service]["agents"])
    t = wait_for(flagged, bound + 30, step=0.5)
    FARMS[service].resume()
    record("fake farm goes silent", t is not None and t <= bound,
           f"farm_stale with agents kept after {'never' if t is None else f'{t:.1f}s'} (bound {bound}s)")
    wait_for(lambda: not (board("grange-a") or {}).get(service, {}).get("farm_stale", True), 10)


def kill_scenario() -> None:
    service = "grange-b"
    docker("kill", container(service))
    bound = EXPIRE_SECONDS + 15
    t = wait_for(lambda: gone_everywhere(service), bound + 30, step=0.5)
    record("kill one grange", t is not None and t <= bound,
           f"gone from all boards after {'never' if t is None else f'{t:.1f}s'} (bound {bound}s)")
    docker("start", container(service))
    FARMS[service].resume()
    wait_for(lambda: all(service in (board(s) or {}) for s in others(service)), 60)


def restart_scenario() -> None:
    service = "grange-d"
    farm = FARMS[service]
    marker = f"after-restart-{int(time.time())}"
    absent = []
    farm.silence()
    killed = time.monotonic()
    docker("kill", container(service))
    docker("start", container(service))
    served = None
    while time.monotonic() - killed < 40:
        for s in others(service):
            b = board(s)
            if b is not None and service not in b:
                absent.append((round(time.monotonic() - killed, 1), s))
        if served is None and serving(service):
            served = time.monotonic()
            farm.set([(marker, "busy")])
            farm.resume()
        if served is not None and all(any(a["name"] == marker for a in (board(s) or {}).get(service, {})
                                          .get("agents", [])) for s in others(service)):
            break
        time.sleep(0.2)
    if served is None:
        return record("kill, serving again within 20 s", False, "never served again")
    t = time.monotonic() - served
    ok = served - killed <= 20 and t <= 5 and not absent
    record("kill, serving again within 20 s", ok,
           f"served {served - killed:.1f}s after the kill; new record on all boards {t:.2f}s later "
           f"(bound 5s); absent from a board {len(absent)} time(s)")


def clean_stop_scenario() -> None:
    service = "grange-c"
    started = time.monotonic()
    docker("stop", "-t", "10", container(service))
    stopped = time.monotonic() - started
    t = wait_for(lambda: gone_everywhere(service), 10, step=0.05)
    total = None if t is None else stopped + t
    record("clean stop", total is not None and total <= 3,
           f"gone from all boards {'never' if total is None else f'{total:.2f}s'} after SIGTERM "
           f"(bound 3s; docker stop took {stopped:.2f}s)")
    docker("start", container(service))
    wait_for(lambda: all(service in (board(s) or {}) for s in others(service)), 60)


def pause_scenario() -> None:
    service = "grange-b"
    docker("pause", container(service))
    removed = wait_for(lambda: gone_everywhere(service), EXPIRE_SECONDS + 30, step=0.5)
    time.sleep(5)                                       # well past EXPIRE_SECONDS
    docker("unpause", container(service))
    t = wait_for(lambda: all(service in (board(s) or {}) for s in others(service)), 20, step=0.1)
    ok = removed is not None and t is not None and t <= 5
    record("pause past EXPIRE_SECONDS, then unpause", ok,
           f"removed after {'never' if removed is None else f'{removed:.1f}s'}; readmitted "
           f"{'never' if t is None else f'{t:.2f}s'} after unpause (bound 5s)")


def gossip_status_from(service: str, target: str) -> int:
    probe = ("import json,urllib.request,urllib.error\n"
             "b=json.dumps({'proto':1,'group':'default','from':'probe','digest':{},'records':[]}).encode()\n"
             f"r=urllib.request.Request('http://{target}:7789/v1/gossip',data=b,method='POST')\n"
             "try:\n  print(urllib.request.urlopen(r,timeout=3).status)\n"
             "except urllib.error.HTTPError as e:\n  print(e.code)\n"
             "except OSError:\n  print(0)\n")
    out = run(["docker", "exec", container(service), "python3", "-c", probe], check=False).stdout.strip()
    return int(out or 0)


def stale_scenario() -> None:
    service = "grange-e"
    before = gossip_status_from("grange-a", service)
    status, _ = http_json(f"{MOCK}/control/tag", "POST", {"sha": "f" * 40})
    if status != 200:
        return record("stale image", False, "could not move the mock tag")
    went_stale = wait_for(lambda: http_json(f"http://127.0.0.1:{PORTS[service]}/health")[0] == 503, 15, step=0.2)
    gossip = gossip_status_from("grange-a", service)
    bound = EXPIRE_SECONDS + 15
    aged = wait_for(lambda: gone_everywhere(service), bound + 30, step=0.5)
    ok = before == 200 and went_stale is not None and gossip == 503 and aged is not None and aged <= bound
    record("stale image", ok,
           f"gossip {before} before, {gossip} after the tag moved; stale after "
           f"{'never' if went_stale is None else f'{went_stale:.1f}s'}; aged out of all boards after "
           f"{'never' if aged is None else f'{aged:.1f}s'} (bound {bound}s)")


def main() -> int:
    try:
        up()
        change_scenario("change on one grange")
        farm_silent_scenario()
        kill_scenario()
        restart_scenario()
        clean_stop_scenario()
        pause_scenario()
        stale_scenario()
        up(fanout="2")
        change_scenario("change with FANOUT=2", service="grange-c")
    except Exception as e:
        record("harness", False, f"{type(e).__name__}: {e}")
    finally:
        logs = run(COMPOSE + ["logs", "--no-color", "--tail", "40"], check=False).stdout
        down()
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
