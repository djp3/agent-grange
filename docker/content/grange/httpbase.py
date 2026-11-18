"""What the local API and the gossip endpoint share: JSON replies, bounded
body reads, and the stale-image refusal (R5.16)."""
from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .freshness_watch import stale_body
from .model import ValidationError, encode_json


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "agent-grange"
    sys_version = ""
    timeout = 40                      # socket reads; the long-poll wait is not a read
    fresh_cfg = None                  # freshness.Config, set by the factory
    fresh_state = None                # FreshnessState, set by the factory
    quiet_paths = ("/health",)

    def send_json(self, code: int, body, close: bool = False, headers: dict = None) -> None:
        payload = encode_json(body) if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def send_error_json(self, code: int, message: str, close: bool = False) -> None:
        self.send_json(code, {"error": message}, close=close)

    def refuse_if_stale(self) -> bool:
        """While the image is stale every route answers 503 (R5.16, R6.18)."""
        if self.fresh_state is None:
            return False
        stale, info = self.fresh_state.snapshot()
        if stale is None:
            return False
        self.send_json(503, stale_body(self.fresh_cfg, stale, info), close=True,
                       headers={"Retry-After": "60"})
        return True

    def read_body(self, limit: int) -> bytes:
        """The declared length is checked against `limit` before anything is
        read, so no request can make the server read without bound (R9.10)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise ValidationError(400, "chunked bodies are not supported; send Content-Length")
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.strip().isdigit():
            raise ValidationError(400, "Content-Length is required")
        length = int(raw)
        if length > limit:
            raise ValidationError(413, f"body of {length} bytes exceeds the {limit}-byte limit")
        return self.rfile.read(length)

    def log_message(self, fmt, *args):
        # Quiet: healthchecks hit /health every few seconds and long-polls are routine.
        pass

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)
