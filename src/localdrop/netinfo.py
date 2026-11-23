"""Find the addresses other devices on the Wi-Fi can use to reach us."""

from __future__ import annotations

import ipaddress
import socket

# UDP "connect" never sends a packet, it only asks the OS which local address
# it would use for that route - works without internet access.
_PROBES = ("10.255.255.255", "192.168.255.255", "172.31.255.255", "8.8.8.8",
           "192.168.43.1", "192.168.49.1", "192.168.137.1")


def _probe(target: str) -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect((target, 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _rank(ip: str) -> int:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return 9
    if addr.is_loopback or addr.is_unspecified:
        return 9
    if addr.is_link_local:
        return 5
    if addr.is_private:
        return 0 if ip.startswith("192.168.") else 1
    return 3


def local_ips() -> list[str]:
    """LAN IPv4 addresses, best candidate first. Never empty."""
    found: list[str] = []
    for target in _PROBES:
        ip = _probe(target)
        if ip and ip not in found:
            found.append(ip)
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    usable = [ip for ip in found if _rank(ip) < 9]
    usable.sort(key=_rank)  # stable: probe order is kept inside one rank
    return usable or ["127.0.0.1"]
