"""Tag discovery and the identity check against a fake tailscaled LocalAPI
on a Unix socket (R6.8, R5.13). The real daemon was checked by hand in the
phase 5 spike; see docs/headscale.md."""
from __future__ import annotations

import json
import os
import socketserver
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from grange.discovery import LocalAPI, StaticDiscovery, TailscaleDiscovery, is_tailnet_ip, wait_for_tailnet

STATUS = {
    "Self": {"HostName": "grange-djp3", "TailscaleIPs": ["100.64.0.5", "fd7a:115c:a1e0::5"], "Tags": ["tag:grange"]},
    "Peer": {
        "k1": {"HostName": "grange-alice", "TailscaleIPs": ["100.64.0.6", "fd7a:115c:a1e0::6"],
               "Online": True, "Tags": ["tag:grange"]},
        "k2": {"HostName": "grange-frank", "TailscaleIPs": ["100.64.0.3"], "Online": False,
               "Tags": ["tag:grange", "tag:other"]},
        "k3": {"HostName": "laptop-two", "TailscaleIPs": ["100.64.0.9"], "Online": True},
        "k4": {"HostName": "grange-v6only", "TailscaleIPs": ["fd7a:115c:a1e0::7"], "Online": True,
               "Tags": ["tag:grange"]},
    },
}
WHOIS = {"100.64.0.6": {"Node": {"Name": "grange-alice", "Tags": ["tag:grange"]}},
         "100.64.0.9": {"Node": {"Name": "laptop-two"}}}


class FakeDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Host")))
            if self.headers.get("Origin") or self.headers.get("Host") != "local-tailscaled.sock":
                return self._send(403, b"invalid localapi request\n")
            if state.get("broken"):
                return self._send(500, b"oops")
            url = urlsplit(self.path)
            if url.path == "/localapi/v0/status":
                return self._send(200, json.dumps(state["status"]).encode())
            if url.path == "/localapi/v0/whois":
                addr = parse_qs(url.query)["addr"][0]
                who = WHOIS.get(addr)
                return self._send(200, json.dumps(who).encode()) if who else self._send(404, b"no match\n")
            self._send(404, b"")

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"
    return Handler


class TailscaleDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "tailscaled.sock")
        self.state = {"requests": [], "status": STATUS}
        self.server = FakeDaemon(self.path, make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.d = TailscaleDiscovery(LocalAPI(self.path), "tag:grange", 7789)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.unlink(self.path)
        os.rmdir(self.dir)

    def test_peers_are_tagged_devices_with_ipv4_and_their_hint(self):
        self.assertEqual(self.d.peers(), {"100.64.0.6:7789": True, "100.64.0.3:7789": False})
        self.assertEqual(self.d.hostnames, {"100.64.0.6:7789": "grange-alice", "100.64.0.3:7789": "grange-frank"})
        self.assertEqual(self.state["requests"][0], ("/localapi/v0/status", "local-tailscaled.sock"))

    def test_own_ipv4(self):
        self.assertEqual(self.d.own_ipv4(), "100.64.0.5")

    def test_whois_accepts_only_the_tag(self):
        self.assertTrue(self.d.is_grange("100.64.0.6"))
        self.assertFalse(self.d.is_grange("100.64.0.9"))      # a device without the tag
        self.assertFalse(self.d.is_grange("100.64.0.99"))     # unknown to the daemon

    def test_failure_to_answer_is_rejection(self):
        self.state["broken"] = True
        self.assertFalse(self.d.is_grange("100.64.0.6"))
        self.server.shutdown()
        self.assertFalse(TailscaleDiscovery(LocalAPI(os.path.join(self.dir, "none.sock")), "tag:grange",
                                            7789).is_grange("100.64.0.6"))

    def test_positive_whois_is_cached_briefly(self):
        self.assertTrue(self.d.is_grange("100.64.0.6"))
        n = len(self.state["requests"])
        self.assertTrue(self.d.is_grange("100.64.0.6"))
        self.assertEqual(len(self.state["requests"]), n)

    def test_wait_for_tailnet(self):
        self.assertEqual(wait_for_tailnet(self.d, 1, lambda m: None), "100.64.0.5")
        self.state["status"] = {"Self": {"TailscaleIPs": []}, "Peer": {}}
        logs = []
        self.assertIsNone(wait_for_tailnet(self.d, 0.6, logs.append))
        self.assertTrue(logs and "gave up" in logs[0])

    def test_a_different_tag(self):
        d = TailscaleDiscovery(LocalAPI(self.path), "tag:other", 7790)
        self.assertEqual(d.peers(), {"100.64.0.3:7790": False})


class HelpersTest(unittest.TestCase):
    def test_tailnet_range(self):
        self.assertTrue(is_tailnet_ip("100.64.0.1"))
        self.assertTrue(is_tailnet_ip("100.127.255.254"))
        for ip in ("100.128.0.1", "172.29.207.3", "127.0.0.1", "not-an-ip", "fd7a:115c:a1e0::1"):
            self.assertFalse(is_tailnet_ip(ip), ip)

    def test_static(self):
        self.assertEqual(StaticDiscovery(("a:1", "b:2")).peers(), {"a:1": True, "b:2": True})


if __name__ == "__main__":
    unittest.main()
