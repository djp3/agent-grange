"""POST /v1/gossip: one endpoint, one body schema, two uses (5.3).

  push    records in, no digest: merge, answer 204 (R5.10)
  repair  digest in, no records: merge the digest for liveness, answer 200
          with our digest plus every body the requester lacks or holds older

Checks, in order, before any field is used: the stale image (503, R5.16),
the source address (403, R5.12 or R6.9), the size cap (413, R5.17), JSON
(400), proto and group (409, R5.11), the schema and caps (400, 413), and in
tailnet mode the identity check (403, R5.13). Each refusal kind is logged
once per peer and counted in /health.

`GossipApp.handle` is the whole of it without HTTP, so the simulation drives
exactly what the HTTP handler does. `parse_reply` is the client side.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional
from urllib.parse import urlsplit

from .config import MAX_GOSSIP_BYTES, PROTO
from .httpbase import JsonHandler
from .model import ValidationError, decode_json, gossip_header, parse_gossip


class GossipApp:
    def __init__(self, store, core, stats, cfg, allow_source: Callable[[str], bool],
                 identity: Optional[Callable[[str], bool]] = None, log: Callable[[str], None] = None):
        self.store = store
        self.core = core
        self.stats = stats
        self.cfg = cfg
        self.allow_source = allow_source
        self.identity = identity
        self.log = log or (lambda m: None)
        self._logged: set = set()
        self._lock = threading.Lock()

    def _refuse(self, kind: str, source: str, status: int, message: str, counter: Optional[str] = None):
        if counter is not None and self.stats is not None:
            self.stats.count(counter)
        with self._lock:
            first = (kind, source) not in self._logged
            self._logged.add((kind, source))
        if first:
            self.log(f"gossip: refused {kind} from {source} with {status}: {message} (logged once per peer)")
        return status, {"error": message}

    def handle(self, source_ip: str, raw: bytes):
        """(status, reply body or None) for one request from `source_ip`."""
        if not self.allow_source(source_ip):
            return self._refuse("source", source_ip, 403, "source is not a grange address", "identity")
        if len(raw) > MAX_GOSSIP_BYTES:
            return self._refuse("size", source_ip, 413, "body exceeds 1 MiB", "overflow")
        try:
            obj = decode_json(raw)
            proto, group = gossip_header(obj)
        except ValidationError as e:
            return e.status, {"error": e.message}
        if proto != PROTO:
            return self._refuse("proto", source_ip, 409, f"proto {proto!r}, this grange speaks {PROTO}", "proto")
        if group != self.cfg.group:
            return self._refuse("group", source_ip, 409, f"group {group!r} is not {self.cfg.group!r}", "group")
        try:
            msg = parse_gossip(obj)
        except ValidationError as e:
            if e.status == 413:
                return self._refuse("caps", source_ip, 413, e.message, "overflow")
            return e.status, {"error": e.message}
        for rec in msg.records:
            if rec.proto != PROTO or rec.group != self.cfg.group:
                kind = "proto" if rec.proto != PROTO else "group"
                return self._refuse(kind, source_ip, 409, f"a body carries {kind} that does not match", kind)
        if msg.digest is not None and msg.records:
            return 400, {"error": "a repair request carries a digest and no records; this has both"}
        if self.identity is not None and not self.identity(source_ip):
            return self._refuse("identity", source_ip, 403, "source device does not carry the grange tag",
                                "identity")

        addr = self.core.address_for_source(source_ip) if self.core is not None else None
        addr = addr or source_ip
        if msg.digest is None:
            self.store.take_push(msg, source=addr)
            reply = None
            status = 204
        else:
            reply = self.store.answer_repair(msg, source=addr).to_json()
            status = 200
        if self.core is not None:
            self.core.on_incoming(addr, msg.sender)
        elif self.stats is not None:
            self.stats.gossip_ok()
        return status, reply


def parse_reply(raw: bytes, cfg):
    """A repair response, validated as strictly as a request. None if unusable."""
    try:
        obj = decode_json(raw)
        proto, group = gossip_header(obj)
        if proto != PROTO or group != cfg.group:
            return None
        msg = parse_gossip(obj)
    except ValidationError:
        return None
    if msg.digest is None:
        return None
    if any(r.proto != PROTO or r.group != cfg.group for r in msg.records):
        return None
    return msg


def make_handler(app: GossipApp, fresh_cfg=None, fresh_state=None):
    stale_cfg, stale_state = fresh_cfg, fresh_state

    class GossipHandler(JsonHandler):
        fresh_cfg = stale_cfg
        fresh_state = stale_state

        def do_POST(self):
            if self.refuse_if_stale():
                return
            if urlsplit(self.path).path != "/v1/gossip":
                return self.send_error_json(404, "no such route")
            source = self.client_address[0]
            if not app.allow_source(source):         # before reading anything
                status, body = app.handle(source, b"")
                return self.send_json(status, body, close=True)
            try:
                raw = self.read_body(MAX_GOSSIP_BYTES)
            except ValidationError as e:
                if e.status == 413:
                    status, body = app._refuse("size", source, 413, e.message, "overflow")
                    return self.send_json(status, body, close=True)
                return self.send_error_json(e.status, e.message, close=True)
            status, body = app.handle(source, raw)
            self.send_json(status, body)

        def do_GET(self):
            if self.refuse_if_stale():
                return
            self.send_error_json(405, "/v1/gossip accepts POST")

        do_PUT = do_DELETE = do_GET

    return GossipHandler
