"""The local API, on loopback and the container's bridge address (5.1, 5.2, 5.4).

  PUT /v1/farm      farm's complete list of sessions; 204 (R5.1 to R5.3)
  GET /v1/agents    the whole merged picture, long-polled (R5.5 to R5.9)
  GET /health       freshness plus the mesh summary (R5.14)
  GET /version      the baked git metadata (R5.15)

Every route answers 503 while the image is stale (R5.16).
"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlsplit

from .config import MAX_FARM_BODY_BYTES, WAIT_MAX_SECONDS
from .httpbase import JsonHandler
from .model import ValidationError, decode_json, parse_farm_push

ROUTES = {"/v1/farm": ("PUT",), "/v1/agents": ("GET",), "/health": ("GET",), "/version": ("GET",)}


class LocalApp:
    """What the handler needs; one per process."""

    def __init__(self, store, stats, build: dict, fresh_cfg=None, fresh_state=None):
        self.store = store
        self.stats = stats
        self.build = build
        self.fresh_cfg = fresh_cfg
        self.fresh_state = fresh_state

    def health(self) -> dict:
        _, info = self.fresh_state.snapshot() if self.fresh_state else (None, {})
        store = self.store.health()
        body = {"status": "ok", "grange": self.store.grange_id, "freshness": info,
                "cursor": store["cursor"], "incarnation": store["incarnation"]}
        body.update(self.stats.snapshot(store_overflow=store["overflow_ids"]))
        body["duplicate_id"] = store["duplicate_id"]
        return body


def parse_long_poll(query: str):
    """(after, wait) from the query string, or ValidationError 400 (R5.7)."""
    params = parse_qs(query, keep_blank_values=True)
    after = None
    if "after" in params:
        raw = params["after"][-1].strip()
        if not re.fullmatch(r"\d+", raw):
            raise ValidationError(400, "after must be a non-negative integer")
        after = int(raw)
    wait = WAIT_MAX_SECONDS
    if "wait" in params:
        raw = params["wait"][-1].strip()
        try:
            wait = float(raw)
        except ValueError:
            raise ValidationError(400, "wait must be a non-negative number of seconds") from None
        if math.isnan(wait) or wait < 0:
            raise ValidationError(400, "wait must be a non-negative number of seconds")
        wait = min(wait, WAIT_MAX_SECONDS)
    if not after:                                    # absent or 0: answer at once
        wait = 0.0
    return after, wait


def make_handler(app: LocalApp):
    class LocalHandler(JsonHandler):
        fresh_cfg = app.fresh_cfg
        fresh_state = app.fresh_state

        def _route(self, method: str):
            path = urlsplit(self.path).path
            allowed = ROUTES.get(path)
            if allowed is None:
                self.send_error_json(404, f"no route {path}")
                return None
            if method not in allowed:
                self.send_error_json(405, f"{path} accepts {', '.join(allowed)}")
                return None
            return path

        def do_GET(self):
            if self.refuse_if_stale():
                return
            path = self._route("GET")
            if path == "/health":
                self.send_json(200, app.health())
            elif path == "/version":
                self.send_json(200, app.build)
            elif path == "/v1/agents":
                try:
                    after, wait = parse_long_poll(urlsplit(self.path).query)
                except ValidationError as e:
                    return self.send_error_json(e.status, e.message)
                picture = app.store.wait_for(after, wait)
                if self.refuse_if_stale():            # the image went stale while we waited
                    return
                self.send_json(200, picture)

        def do_PUT(self):
            if self.refuse_if_stale():
                return
            if self._route("PUT") != "/v1/farm":
                return
            try:
                raw = self.read_body(MAX_FARM_BODY_BYTES)
                agents = parse_farm_push(decode_json(raw))
            except ValidationError as e:
                return self.send_error_json(e.status, e.message, close=e.status == 413)
            if app.store.set_agents(agents):
                self.log(f"farm: {len(agents)} agent(s), content change, rev {app.store.rev}")
            self.send_json(204, None)

        def do_POST(self):
            if self.refuse_if_stale():
                return
            self._route("POST")

        do_DELETE = do_PATCH = do_POST

    return LocalHandler
