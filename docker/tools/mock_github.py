#!/usr/bin/env python3
"""An ETag-aware stand-in for the two GitHub endpoints the freshness check
uses, so the harness can make a grange stale without touching GitHub (R10.2).

  GET  /repos/{owner}/{repo}/git/ref/tags/{tag}   the tag's commit; 304 on a matching If-None-Match
  GET  /repos/{owner}/{repo}/compare/{base}...{head}
  POST /control/tag   {"sha": "..."}               move the tag
  GET  /control/tag                                 where it points

The tag starts at MOCK_TAG_SHA (the image's commit, so the grange starts
FRESH). Moving it anywhere else makes compare answer "ahead": the tag is
newer than the image, so the image is STALE. Python 3.9 or later, standard
library only (R8.8); it also runs inside the grange image for the harness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"sha": os.environ.get("MOCK_TAG_SHA", "0" * 40), "image": os.environ.get("MOCK_TAG_SHA", "0" * 40)}
LOCK = threading.Lock()

REF = re.compile(r"^/repos/[^/]+/[^/]+/git/ref/tags/(?P<tag>.+)$")
COMPARE = re.compile(r"^/repos/[^/]+/[^/]+/compare/(?P<base>[0-9a-f]+)\.\.\.(?P<head>[0-9a-f]+)$")


def etag(sha: str) -> str:
    return '"' + hashlib.sha1(sha.encode()).hexdigest() + '"'


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body=None, headers=None) -> None:
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        with LOCK:
            sha, image = STATE["sha"], STATE["image"]
        if self.path == "/control/tag":
            return self._send(200, {"sha": sha, "image": image})
        m = REF.match(self.path)
        if m:
            tag_etag = etag(sha)
            if self.headers.get("If-None-Match") == tag_etag:
                return self._send(304, None, {"ETag": tag_etag})
            return self._send(200, {"ref": f"refs/tags/{m.group('tag')}",
                                    "object": {"type": "commit", "sha": sha}}, {"ETag": tag_etag})
        m = COMPARE.match(self.path)
        if m:
            base, head = m.group("base"), m.group("head")
            if base == head:
                return self._send(200, {"status": "identical", "ahead_by": 0, "behind_by": 0})
            if base == image and head == sha:
                return self._send(200, {"status": "ahead", "ahead_by": 1, "behind_by": 0})
            return self._send(404, {"message": "Not Found"})
        self._send(404, {"message": "Not Found"})

    def do_POST(self):
        if self.path != "/control/tag":
            return self._send(404, {"message": "Not Found"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            STATE["sha"] = body.get("sha", STATE["sha"])
            if "image" in body:
                STATE["image"] = body["image"]
            out = dict(STATE)
        self._send(200, out)

    def log_message(self, fmt, *args):
        pass


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args(argv[1:])
    print(f"mock GitHub on {args.bind}:{args.port}, tag at {STATE['sha'][:12]}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
