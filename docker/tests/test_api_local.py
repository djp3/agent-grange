"""The local API on an in-process server and an ephemeral port (plan phase 2)."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import freshness
from grange.api_local import LocalApp, make_handler, parse_long_poll
from grange.clock import SystemClock
from grange.config import Config
from grange.freshness_watch import FreshnessState
from grange.httpbase import Server
from grange.model import ValidationError
from grange.state import Store
from grange.stats import Stats

CONTENT = Path(freshness.__file__).resolve().parent


class LocalApiTest(unittest.TestCase):
    def setUp(self):
        self.clock = SystemClock()
        self.store = Store(Config(grange_id="djp3"), self.clock, log=lambda m: None)
        self.fresh_cfg = freshness.Config.from_env({"GIT_COMMIT": "a" * 40, "GIT_REPO": "o/r",
                                                    "CHECK_TAG": "agent-grange-latest"})
        self.fresh_state = FreshnessState(30)
        build = {"repo": "o/r", "branch": "main", "commit": "a" * 40, "dirty": False}
        self.app = LocalApp(self.store, Stats(self.clock), build, self.fresh_cfg, self.fresh_state)
        self.server = Server(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None, raw=None, timeout=40):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        payload = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else None)

    def push(self, agents):
        return self.request("PUT", "/v1/farm", {"agents": agents})

    # ---------------------------------------------------------------- farm push
    def test_push_is_204_and_shows_on_the_board(self):
        status, body = self.push([{"owner": "djp3", "name": "proj-86-pr-98 fix tests", "state": "busy"}])
        self.assertEqual((status, body), (204, None))
        status, picture = self.request("GET", "/v1/agents")
        self.assertEqual(status, 200)
        self.assertEqual(picture["grange"], "djp3")
        self.assertEqual(picture["granges"][0]["agents"][0]["name"], "proj-86-pr-98 fix tests")
        self.assertEqual(picture["granges"][0]["farm_age_s"], 0.0)

    def test_malformed_push_is_400(self):
        for raw in (b"{", b"[]", b'{"agents": 3}', b'{"agents":[{"owner":"o"}]}', b"NaN"):
            status, body = self.request("PUT", "/v1/farm", raw=raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("error", body)

    def test_oversize_push_is_413(self):
        too_many = [{"owner": "o", "name": f"n{i}", "state": "s"} for i in range(201)]
        self.assertEqual(self.push(too_many)[0], 413)
        self.assertEqual(self.push([{"owner": "o", "name": "x" * 257, "state": "s"}])[0], 413)

    def test_declared_length_over_the_cap_is_413_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("PUT", "/v1/farm")
        conn.putheader("Content-Length", str((1 << 20) + 1))
        conn.endheaders()                       # and send no body at all
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_missing_length_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("PUT", "/v1/farm")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("GET", "/v1/farm")[0], 405)
        self.assertEqual(self.request("POST", "/v1/agents", {})[0], 405)

    # ---------------------------------------------------------------- long-poll
    def test_push_wakes_a_waiting_long_poll(self):
        cursor = self.request("GET", "/v1/agents")[1]["cursor"]
        result = {}

        def poll():
            started = time.monotonic()
            result["body"] = self.request("GET", f"/v1/agents?after={cursor}&wait=10")[1]
            result["done"] = time.monotonic()
            result["started"] = started
        t = threading.Thread(target=poll)
        t.start()
        time.sleep(0.3)                          # the poll is waiting
        pushed = time.monotonic()
        self.push([{"owner": "djp3", "name": "n", "state": "busy"}])
        t.join(5)
        self.assertLess(result["done"] - pushed, 0.1)         # within a few milliseconds, generously
        self.assertNotEqual(result["body"]["cursor"], cursor)

    def test_long_poll_with_nothing_happening_returns_at_wait(self):
        cursor = self.request("GET", "/v1/agents")[1]["cursor"]
        started = time.monotonic()
        status, body = self.request("GET", f"/v1/agents?after={cursor}&wait=0.5")
        elapsed = time.monotonic() - started
        self.assertEqual((status, body["cursor"]), (200, cursor))
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertLess(elapsed, 2)

    def test_after_other_than_the_cursor_returns_at_once(self):
        cursor = self.request("GET", "/v1/agents")[1]["cursor"]
        for after in (0, cursor - 1 if cursor > 1 else 0, cursor + 1, cursor + 500):
            started = time.monotonic()
            self.request("GET", f"/v1/agents?after={after}&wait=10")
            self.assertLess(time.monotonic() - started, 1, after)
        started = time.monotonic()
        self.request("GET", "/v1/agents?wait=10")              # no after: at once
        self.assertLess(time.monotonic() - started, 1)

    def test_bad_after_or_wait_is_400(self):
        for query in ("after=-1", "after=x", "after=1.5", "after=", "wait=-1", "wait=soon", "wait=nan",
                      "after=1&wait=-0.5"):
            status, body = self.request("GET", f"/v1/agents?{query}")
            self.assertEqual(status, 400, query)

    def test_wait_parsing(self):
        self.assertEqual(parse_long_poll("after=5"), (5, 30.0))                # default 30
        self.assertEqual(parse_long_poll("after=5&wait=90"), (5, 30.0))        # capped at 30
        self.assertEqual(parse_long_poll("after=5&wait=0"), (5, 0.0))
        self.assertEqual(parse_long_poll("after=5&wait=1e400"), (5, 30.0))
        self.assertEqual(parse_long_poll("after=0&wait=10"), (0, 0.0))
        self.assertEqual(parse_long_poll("wait=10"), (None, 0.0))
        with self.assertRaises(ValidationError):
            parse_long_poll("after=%2B3")             # a literal +3

    # ------------------------------------------------------------ health, stale
    def test_health_fields(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        for key in ("freshness", "cursor", "incarnation", "peers_discovered", "peers_live",
                    "last_gossip_age_s", "rejected", "duplicate_id"):
            self.assertIn(key, body)
        self.assertEqual(body["rejected"], {"proto": 0, "group": 0, "identity": 0, "overflow": 0})
        self.assertIsNone(body["last_gossip_age_s"])
        self.assertEqual(body["incarnation"], self.store.incarnation)
        self.assertFalse(body["duplicate_id"])

    def test_version(self):
        self.assertEqual(self.request("GET", "/version")[1]["commit"], "a" * 40)

    def test_every_route_is_503_while_stale(self):
        stale = freshness.Result(freshness.Verdict.STALE, "image is 2 commit(s) behind", ["x"], "b" * 40)
        self.fresh_state.update(stale, changed=True)
        for method, path, body in (("GET", "/health", None), ("GET", "/version", None),
                                   ("GET", "/v1/agents", None), ("PUT", "/v1/farm", {"agents": []}),
                                   ("GET", "/nope", None)):
            status, reply = self.request(method, path, body)
            self.assertEqual(status, 503, path)
            self.assertEqual(reply["tag_commit"], "b" * 40)

    def test_healthcheck_script_passes_and_fails_with_a_reason(self):
        env = dict(os.environ, PORT=str(self.port))
        ok = subprocess.run([sys.executable, str(CONTENT / "healthcheck.py")], env=env,
                            capture_output=True, text=True, timeout=10)
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "ok"))
        stale = freshness.Result(freshness.Verdict.STALE, "image is 2 commit(s) behind", [], "b" * 40)
        self.fresh_state.update(stale, changed=True)
        bad = subprocess.run([sys.executable, str(CONTENT / "healthcheck.py")], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("HTTP 503: image is 2 commit(s) behind", bad.stdout)


if __name__ == "__main__":
    unittest.main()
