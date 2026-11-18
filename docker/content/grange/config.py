"""Configuration: every grange key of requirements section 7, with its default.

Configuration is environment only. An empty value means the default. The
defaults live here and nowhere else; compose passes keys through from `.env`
without repeating them. The freshness keys are read by freshness.py, as
before. TS_AUTHKEY and HEADSCALE_URL are the sidecar's, not the grange's.

The caps of R4.3 and R5.17 and the resume gap of R6.22 are constants, not keys.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

# ------------------------------------------------------------- protocol constants
PROTO = 1

MAX_STRING_BYTES = 256          # R4.3: every string field
MAX_AGENTS = 200                # R4.3: agents in one record
MAX_FARM_BODY_BYTES = 1 << 20   # R5.1: PUT /v1/farm
MAX_GOSSIP_BYTES = 1 << 20      # R5.17: one gossip request or response
MAX_BODIES_PER_MESSAGE = 200    # R5.17
MAX_DIGEST_ENTRIES = 1000       # R5.17
MAX_RECORDS = 1000              # R5.17: records held, the own one included
RESUME_GAP_SECONDS = 5.0        # R6.22
WAIT_MAX_SECONDS = 30.0         # R5.7: long-poll wait default and maximum
STARTUP_REPAIR_SECONDS = 5.0    # 6.1 step 5
LEAVE_WAIT_SECONDS = 2.0        # R6.16
TAILNET_WAIT_SECONDS = 60.0     # 6.1 step 3
TS_SOCKET_PATH = "/var/run/tailscale/tailscaled.sock"   # where compose mounts the sidecar's socket

EX_CONFIG = 78                  # same exit code as the freshness refusal

# 1 to 56 characters from lowercase letters, digits, and hyphens, neither
# starting nor ending with a hyphen, so grange-<id> is a valid hostname.
GRANGE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,54}[a-z0-9])?$")


class ConfigError(ValueError):
    """A one-line reason the grange refuses to start."""


@dataclass(frozen=True)
class Config:
    grange_id: str
    port: int = 7788                    # GRANGE_PORT: published on the host's loopback
    gossip_port: int = 7789             # GRANGE_GOSSIP_PORT
    group: str = "default"              # GRANGE_GROUP
    ts_tag: str = "tag:grange"          # TS_TAG
    peers: tuple = ()                   # GRANGE_PEERS: ("host:port", ...); plain mode when non-empty
    heartbeat_s: float = 10.0           # HEARTBEAT_SECONDS
    farm_stale_s: float = 60.0          # FARM_STALE_SECONDS
    expire_s: float = 60.0              # EXPIRE_SECONDS
    forget_s: float = 600.0             # FORGET_SECONDS
    repair_s: float = 3.0               # REPAIR_SECONDS
    discovery_s: float = 30.0           # DISCOVERY_SECONDS
    fanout: int = 5                     # FANOUT
    connect_timeout_s: float = 1.0      # CONNECT_TIMEOUT_SECONDS
    exchange_timeout_s: float = 3.0     # EXCHANGE_TIMEOUT_SECONDS
    api_port: int = 8080                # PORT: the API port inside the container (image ENV)

    @property
    def plain_mode(self) -> bool:
        """R6.9: GRANGE_PEERS set means no tailnet, testing only."""
        return bool(self.peers)

    def with_(self, **changes) -> "Config":
        return replace(self, **changes)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        def get(key: str) -> str:
            return (env.get(key) or "").strip()

        gid = get("GRANGE_ID")
        if not gid:
            raise ConfigError("GRANGE_ID is not set; put it in .env (see .env.example)")
        check_grange_id(gid)

        kw = {"grange_id": gid}
        ints = {"GRANGE_PORT": "port", "GRANGE_GOSSIP_PORT": "gossip_port", "FANOUT": "fanout", "PORT": "api_port"}
        floats = {
            "HEARTBEAT_SECONDS": "heartbeat_s", "FARM_STALE_SECONDS": "farm_stale_s",
            "EXPIRE_SECONDS": "expire_s", "FORGET_SECONDS": "forget_s",
            "REPAIR_SECONDS": "repair_s", "DISCOVERY_SECONDS": "discovery_s",
            "CONNECT_TIMEOUT_SECONDS": "connect_timeout_s",
            "EXCHANGE_TIMEOUT_SECONDS": "exchange_timeout_s",
        }
        for key, attr in ints.items():
            raw = get(key)
            if raw:
                kw[attr] = _parse_int(key, raw)
        for key, attr in floats.items():
            raw = get(key)
            if raw:
                kw[attr] = _parse_seconds(key, raw)
        if get("GRANGE_GROUP"):
            kw["group"] = _parse_name("GRANGE_GROUP", get("GRANGE_GROUP"))
        if get("TS_TAG"):
            kw["ts_tag"] = _parse_name("TS_TAG", get("TS_TAG"))
        if get("GRANGE_PEERS"):
            kw["peers"] = parse_peers(get("GRANGE_PEERS"))

        cfg = cls(**kw)
        for attr in ("port", "gossip_port", "api_port"):
            value = getattr(cfg, attr)
            if not 1 <= value <= 65535:
                raise ConfigError(f"{_key_of(attr)}={value} is not a TCP port (1 to 65535)")
        if cfg.fanout < 1:
            raise ConfigError(f"FANOUT={cfg.fanout} must be at least 1")
        return cfg


def check_grange_id(gid: str) -> None:
    if not GRANGE_ID_RE.match(gid):
        raise ConfigError(
            f"GRANGE_ID={gid!r} is invalid: use 1 to 56 lowercase letters, digits, or hyphens, "
            f"not starting or ending with a hyphen")


def parse_peers(raw: str) -> tuple:
    """`host:port` entries separated by commas or whitespace."""
    peers = []
    for item in re.split(r"[\s,]+", raw.strip()):
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ConfigError(f"GRANGE_PEERS entry {item!r} is not host:port")
        peers.append(f"{host}:{int(port)}")
    return tuple(dict.fromkeys(peers))       # drop duplicates, keep order


def _parse_int(key: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} is not an integer") from None


def _parse_seconds(key: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} is not a number of seconds") from None
    if not value > 0 or value != value or value == float("inf"):
        raise ConfigError(f"{key}={raw!r} must be a positive number of seconds")
    return value


def _parse_name(key: str, raw: str) -> str:
    if len(raw.encode()) > MAX_STRING_BYTES:
        raise ConfigError(f"{key} is longer than {MAX_STRING_BYTES} bytes")
    return raw


def _key_of(attr: str) -> str:
    return {"port": "GRANGE_PORT", "gossip_port": "GRANGE_GOSSIP_PORT", "api_port": "PORT"}[attr]
