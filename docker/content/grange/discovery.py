"""Peer discovery providers. Each has `peers() -> {address: online_hint}` and
`hostnames -> {address: hostname}` (a hint only, R6.19).

Tailscale (R6.8): the sidecar's peer list through the daemon's LocalAPI, kept
to devices carrying TS_TAG with a tailnet IPv4 address. Paths and headers as
the phase 5 spike confirmed against tailscale v1.104.7 (docs/headscale.md):

  GET /localapi/v0/status            Self and Peer, each with HostName,
                                     TailscaleIPs, Online, Tags
  GET /localapi/v0/whois?addr=IP     Node.Tags of the device at that address

over the daemon's Unix socket, with Host "local-tailscaled.sock" and no Origin
or Referer header (the daemon answers 403 otherwise). Read access is enough,
so the grange can run as a non-root user (R8.7).

Static (R6.9): plain mode for testing. GRANGE_PEERS replaces tag discovery
entirely, and every listed address counts as online.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
from typing import Optional
from urllib.parse import quote

LOCALAPI_HOST = "local-tailscaled.sock"
TAILNET_V4 = ipaddress.IPv4Network("100.64.0.0/10")      # the tailnet's CGNAT range


class StaticDiscovery:
    def __init__(self, peers):
        self._peers = tuple(peers)
        self.hostnames: dict = {}

    def peers(self) -> dict:
        return {addr: True for addr in self._peers}


class NoDiscovery:
    """A grange with neither GRANGE_PEERS nor a sidecar: nothing to find."""
    hostnames: dict = {}

    def peers(self) -> dict:
        return {}


class LocalAPIError(OSError):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__(LOCALAPI_HOST, timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


class LocalAPI:
    """The sidecar daemon's LocalAPI over its Unix socket."""

    def __init__(self, socket_path: str = "/var/run/tailscale/tailscaled.sock", timeout: float = 2.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def get(self, path: str):
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", path, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read(4 << 20)
        except OSError as e:
            raise LocalAPIError(f"tailscaled LocalAPI {path}: {e}") from None
        finally:
            conn.close()
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise LocalAPIError(f"tailscaled LocalAPI {path}: HTTP {resp.status} {body[:80]!r}")
        try:
            return json.loads(body)
        except ValueError:
            raise LocalAPIError(f"tailscaled LocalAPI {path}: not JSON") from None

    def status(self) -> dict:
        return self.get("/localapi/v0/status") or {}

    def whois(self, ip: str) -> Optional[dict]:
        return self.get(f"/localapi/v0/whois?addr={quote(ip)}")


def _ipv4(ips) -> Optional[str]:
    for ip in ips or ():
        try:
            if isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address):
                return ip
        except ValueError:
            continue
    return None


def _tags(node: dict) -> list:
    tags = node.get("Tags")
    return list(tags) if isinstance(tags, list) else []


class TailscaleDiscovery:
    """Tag discovery (R6.8) and the identity check (R5.13)."""

    def __init__(self, api: LocalAPI, tag: str, gossip_port: int, whois_ttl: float = 10.0):
        self.api = api
        self.tag = tag
        self.port = gossip_port
        self.whois_ttl = whois_ttl
        self.hostnames: dict = {}
        self._whois: dict = {}            # ip -> (accepted, at)
        self._lock = threading.Lock()

    def own_ipv4(self) -> Optional[str]:
        st = self.api.status()
        return _ipv4((st.get("Self") or {}).get("TailscaleIPs"))

    def peers(self) -> dict:
        st = self.api.status()
        out, names = {}, {}
        for peer in (st.get("Peer") or {}).values():
            if self.tag not in _tags(peer):
                continue
            ip = _ipv4(peer.get("TailscaleIPs"))
            if ip is None:
                continue
            addr = f"{ip}:{self.port}"
            out[addr] = bool(peer.get("Online"))
            names[addr] = peer.get("HostName") or ""
        self.hostnames = names
        return out

    def is_grange(self, ip: str) -> bool:
        """True only if the daemon says the device at `ip` carries the tag. A
        failure to answer is a rejection (R5.13)."""
        now = time.monotonic()
        with self._lock:
            hit = self._whois.get(ip)
            if hit is not None and hit[0] and now - hit[1] < self.whois_ttl:
                return True
        try:
            who = self.api.whois(ip)
        except LocalAPIError:
            return False
        ok = bool(who) and self.tag in _tags(who.get("Node") or {})
        with self._lock:
            self._whois[ip] = (ok, now)
        return ok


def is_tailnet_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILNET_V4
    except ValueError:
        return False


def wait_for_tailnet(discovery: TailscaleDiscovery, seconds: float, log) -> Optional[str]:
    """Startup step 3: the sidecar's tailnet IPv4, or None after `seconds`."""
    deadline = time.monotonic() + seconds
    last = None
    while True:
        try:
            ip = discovery.own_ipv4()
            if ip:
                return ip
            last = "no tailnet IPv4 yet"
        except LocalAPIError as e:
            last = str(e)
        if time.monotonic() >= deadline:
            log(f"tailnet: gave up after {seconds:.0f}s: {last}")
            return None
        time.sleep(0.5)
