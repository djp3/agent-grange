"""Which addresses to bind (6.1 step 4): never all interfaces."""
from __future__ import annotations

import ipaddress
import socket
import struct
from typing import Optional

RTF_GATEWAY = 0x2


def default_route_address(route_file: str = "/proc/net/route") -> Optional[str]:
    """This container's address on the interface that carries the default
    route: its Docker bridge address. None where there is no /proc (a Mac
    host running the tests) or no default route."""
    try:
        with open(route_file) as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000" or not int(fields[3], 16) & RTF_GATEWAY:
            continue
        gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        # A UDP connect sends nothing; it only asks the kernel which source
        # address it would use to reach the gateway.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect((gateway, 9))
                return s.getsockname()[0]
            except OSError:
                return None
    return None


def local_api_addresses() -> list:
    """Loopback plus the bridge address, which the published port reaches."""
    addrs = ["127.0.0.1"]
    bridge = default_route_address()
    if bridge and bridge not in addrs:
        addrs.append(bridge)
    return addrs


def local_networks(route_file: str = "/proc/net/route") -> list:
    """The directly connected IPv4 networks of the interface that carries the
    default route: in plain mode, the Docker network the harness granges share
    (R6.9). Empty where there is no /proc."""
    try:
        with open(route_file) as f:
            rows = [line.split() for line in f.read().splitlines()[1:]]
    except OSError:
        return []
    default_ifaces = {r[0] for r in rows if len(r) >= 8 and r[1] == "00000000" and int(r[3], 16) & RTF_GATEWAY}
    nets = []
    for r in rows:
        if len(r) < 8 or r[0] not in default_ifaces or r[1] == "00000000" or r[2] != "00000000":
            continue
        dest = socket.inet_ntoa(struct.pack("<L", int(r[1], 16)))
        mask = socket.inet_ntoa(struct.pack("<L", int(r[7], 16)))
        nets.append(ipaddress.IPv4Network(f"{dest}/{mask}", strict=False))
    return nets


def in_networks(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)
