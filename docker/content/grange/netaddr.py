"""Which addresses to bind (6.1 step 4): never all interfaces."""
from __future__ import annotations

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
