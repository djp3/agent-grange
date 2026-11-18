#!/usr/bin/env python3
"""Runs on the HOST. Fallback only: the supported way in is curl against the
fixed port, 127.0.0.1:${GRANGE_PORT} (R8.3). Finds the grange container
through the Docker Engine API by its label, reads its published host port,
and queries the server.

Standard library only: no docker SDK, no requests. The Engine API is plain
HTTP over a Unix socket, so http.client with a custom connect() is enough.

Usage:
    python3 tools/query_server.py            # GET /
    python3 tools/query_server.py /anything  # GET /anything
"""
from __future__ import annotations   # host Python 3.9 or later (R8.8)

import http.client
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request

LABEL = "agent-grange.service=grange"
CONTAINER_PORT = 8080
TIMEOUT_SECONDS = float(os.environ.get("DISCOVERY_TIMEOUT", "60"))


# ---------------------------------------------------------------- Docker socket

def docker_socket_path() -> str:
    """Honor DOCKER_HOST, then try the usual locations."""
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[len("unix://"):]
    candidates = [
        "/var/run/docker.sock",                               # Linux, Docker Desktop (default socket enabled)
        os.path.expanduser("~/.docker/run/docker.sock"),      # Docker Desktop for Mac
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise RuntimeError("Docker socket not found. Is Docker running? Try: docker context inspect")


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that talks to a Unix socket instead of TCP."""

    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_get(path: str):
    conn = UnixHTTPConnection(docker_socket_path())
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"Docker API {path} -> {resp.status}: {body.decode(errors='replace')}")
        return json.loads(body)
    finally:
        conn.close()


# ------------------------------------------------------------------- discovery

def list_containers(extra_filters: dict | None = None) -> list[dict]:
    """GET /containers/json filtered by our label (and anything extra)."""
    filters = {"label": [LABEL]}
    if extra_filters:
        filters.update(extra_filters)
    query = urllib.parse.urlencode({"filters": json.dumps(filters)})
    return docker_get(f"/containers/json?{query}")


def inspect_health(container_id: str):
    """(status, last_check_output) from GET /containers/{id}/json, or None if no healthcheck."""
    health = docker_get(f"/containers/{container_id}/json").get("State", {}).get("Health")
    if not health:
        return None
    entries = health.get("Log") or []
    last = (entries[-1].get("Output") or "").strip() if entries else ""
    return health.get("Status", ""), last


def host_port_of(container: dict) -> int:
    """Pull the published host port for CONTAINER_PORT out of a /containers/json entry.

    The entry's "Ports" looks like:
      [{"IP": "127.0.0.1", "PrivatePort": 8080, "PublicPort": 55012, "Type": "tcp"}, ...]
    Docker may list IPv4 and IPv6 bindings; prefer IPv4.
    """
    bindings = [
        p for p in container.get("Ports", [])
        if p.get("PrivatePort") == CONTAINER_PORT and p.get("Type") == "tcp" and "PublicPort" in p
    ]
    if not bindings:
        raise RuntimeError(f"container {container['Id'][:12]} has no published binding for {CONTAINER_PORT}/tcp")
    bindings.sort(key=lambda p: ":" in p.get("IP", ""))  # IPv4 first
    return int(bindings[0]["PublicPort"])


def find_server(timeout: float = TIMEOUT_SECONDS) -> str:
    """Return a base URL like http://127.0.0.1:55012 for a healthy container.

    Polls until a container with our label is healthy or the timeout expires.
    """
    deadline = time.time() + timeout
    last_state = "no container with label " + LABEL
    while True:
        healthy = list_containers({"health": ["healthy"]})
        if healthy:
            c = healthy[0]
            port = host_port_of(c)
            name = c["Names"][0].lstrip("/")
            print(f"[discovery] found {name} ({c['Id'][:12]}) healthy, "
                  f"container port {CONTAINER_PORT} -> host port {port}", file=sys.stderr)
            # Always connect to loopback, never to the IP Docker reports (may be 0.0.0.0 or ::).
            return f"http://127.0.0.1:{port}"

        # Not healthy yet: explain what we are waiting on.
        any_match = list_containers()
        if any_match:
            c = any_match[0]
            name = c["Names"][0].lstrip("/")
            health = inspect_health(c["Id"])
            if health and health[0] == "unhealthy":
                # Waiting will not help: the container is up but refusing. Say why and stop.
                raise RuntimeError(f"{name} is running but UNHEALTHY: {health[1] or 'no reason recorded'}")
            last_state = f"{name} is {c['State']} ({c['Status']})"
        if time.time() >= deadline:
            raise TimeoutError(f"gave up after {timeout:.0f}s; last state: {last_state}")
        print(f"[discovery] waiting: {last_state}", file=sys.stderr)
        time.sleep(1)


# ----------------------------------------------------------------------- query

def query(base_url: str, path: str) -> str:
    with urllib.request.urlopen(base_url + path, timeout=5) as resp:
        return resp.read().decode()


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/"
    if not path.startswith("/"):
        path = "/" + path
    try:
        base = find_server()
        print(f"[query] GET {base}{path}", file=sys.stderr)
        print(query(base, path))
        return 0
    except (RuntimeError, TimeoutError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
