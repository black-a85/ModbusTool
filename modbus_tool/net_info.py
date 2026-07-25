"""Read-only view of the PC's network adapters.

Shown for reference on the TCP tab so the user can see the machine's own
IP / subnet / gateway while debugging why a slave can't be reached. This does
NOT change any adapter settings.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

import psutil


@dataclass
class AdapterInfo:
    name: str
    ipv4: str = ""
    netmask: str = ""
    mac: str = ""
    is_up: bool = False
    gateways: list[str] = field(default_factory=list)


def primary_outbound_ip() -> str:
    """The local IP the OS would use to reach the internet (no traffic sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def list_adapters() -> list[AdapterInfo]:
    """Enumerate adapters with their IPv4 address, netmask, MAC and up/down."""
    adapters: dict[str, AdapterInfo] = {}
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()

    for name, addr_list in addrs.items():
        info = AdapterInfo(name=name)
        if name in stats:
            info.is_up = stats[name].isup
        for a in addr_list:
            if a.family == socket.AF_INET:
                info.ipv4 = a.address
                info.netmask = a.netmask or ""
            elif getattr(a, "family", None) == psutil.AF_LINK:
                info.mac = a.address
        adapters[name] = info

    return list(adapters.values())


def summary_text() -> str:
    """A compact multi-line summary suitable for a read-only text box."""
    lines = []
    primary = primary_outbound_ip()
    if primary:
        lines.append(f"Primary outbound IP: {primary}")
        lines.append("")
    for a in list_adapters():
        if not a.ipv4:
            continue
        state = "up" if a.is_up else "down"
        lines.append(f"{a.name}  [{state}]")
        lines.append(f"    IPv4   : {a.ipv4}")
        lines.append(f"    Mask   : {a.netmask}")
        if a.mac:
            lines.append(f"    MAC    : {a.mac}")
        lines.append("")
    if not lines:
        return "No active IPv4 adapters found."
    return "\n".join(lines).rstrip()
