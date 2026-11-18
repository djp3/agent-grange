"""Peer discovery providers. Each has `peers() -> {address: online_hint}` and
`hostnames -> {address: hostname}` (a hint only, R6.19).

Static (R6.9): plain mode for testing. GRANGE_PEERS replaces tag discovery
entirely, and every listed address counts as online. The Tailscale provider
(R6.8) implements the same interface in plan phase 5.
"""
from __future__ import annotations


class StaticDiscovery:
    def __init__(self, peers):
        self._peers = tuple(peers)
        self.hostnames: dict = {}

    def peers(self) -> dict:
        return {addr: True for addr in self._peers}


class NoDiscovery:
    """A lone grange: no GRANGE_PEERS and no tailnet yet."""
    hostnames: dict = {}

    def peers(self) -> dict:
        return {}
