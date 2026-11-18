#!/usr/bin/env python3
"""A scripted stand-in for agent-farm: pushes an agent list to one grange's
PUT /v1/farm, on every change and every 30 seconds as a heartbeat (R5.2),
or falls silent on command. Host-side, Python 3.9 or later, standard library
only (R8.8).

As a module (used by scenario.py):
    farm = FakeFarm("http://127.0.0.1:17881", owner="grange-a")
    farm.start()                      # first push now, then heartbeats
    farm.set([("grange-a-pr-1 task", "waiting")])
    farm.silence(); farm.resume(); farm.stop()

As a command:
    python3 tools/fake_farm.py http://127.0.0.1:7788 djp3 "proj-86-pr-98 fix tests=busy" ...
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

HEARTBEAT_SECONDS = 30.0


class FakeFarm:
    def __init__(self, base_url: str, owner: str, agents=None, heartbeat: float = HEARTBEAT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.agents = list(agents if agents is not None else [(f"{owner}-pr-1 task", "busy")])
        self.heartbeat = heartbeat
        self.silent = False
        self.pushes = 0
        self.errors = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def body(self) -> bytes:
        return json.dumps({"agents": [{"owner": self.owner, "name": n, "state": s}
                                      for n, s in self.agents]}).encode()

    def push(self) -> int:
        req = urllib.request.Request(f"{self.base_url}/v1/farm", data=self.body(), method="PUT",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                self.pushes += 1
                return resp.status
        except urllib.error.HTTPError as e:
            self.errors += 1
            return e.code
        except OSError:
            self.errors += 1
            return 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.silent:
                self.push()
            self._wake.wait(self.heartbeat)
            self._wake.clear()

    def start(self) -> "FakeFarm":
        self._thread = threading.Thread(target=self._loop, name=f"farm-{self.owner}", daemon=True)
        self._thread.start()
        return self

    def set(self, agents) -> None:
        """A change: pushed at once."""
        self.agents = list(agents)
        self._wake.set()

    def silence(self) -> None:
        self.silent = True

    def resume(self) -> None:
        self.silent = False
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    url, owner = argv[1], argv[2]
    agents = [tuple(a.rsplit("=", 1)) if "=" in a else (a, "busy") for a in argv[3:]]
    farm = FakeFarm(url, owner, agents)
    print(f"pushing {len(agents)} agent(s) to {url} every {HEARTBEAT_SECONDS:.0f}s; Ctrl-C to stop", file=sys.stderr)
    farm.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        farm.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
