"""URL safety for the Agent Browser (SSRF / local-access protection).

Every request (navigations, redirects that reach the router, sub-resources) is checked:
  * scheme must be http or https (file:, chrome:, javascript:, ftp:, data: navigations... blocked)
  * the host is resolved and EVERY address must be public: loopback, private (RFC1918), link-local,
    CGNAT, multicast, reserved, unspecified and IPv4-mapped/6to4 forms of those are refused
  * 'localhost' / '*.localhost' / '*.local' / '*.internal' names are refused
  * hosts can be allowed explicitly (allow_hosts) -- used by tests with a local server

Known limitation: Chromium resolves DNS itself, so a host could resolve differently between our
check and Chromium's connection (DNS rebinding window of milliseconds). Resolutions are cached for
60 s to keep that window consistent, and private targets are re-checked on every request.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
_BAD_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")
_cache: dict[str, tuple[float, list[str]]] = {}


def _resolve(host: str) -> list[str]:
    now = time.time()
    hit = _cache.get(host)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
    except OSError:
        addrs = []
    _cache[host] = (now, addrs)
    return addrs


def is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        for inner in (ip.ipv4_mapped, ip.sixtofour):
            if inner is not None:
                return is_public_ip(str(inner))
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or
                (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")))


def check(url: str, allow_hosts: frozenset = frozenset()) -> tuple[bool, str]:
    """(allowed?, reason)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "malformed URL"
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"scheme '{scheme or '?'}:' is not allowed"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False, "no host"
    if host in allow_hosts or f"{host}:{parts.port}" in allow_hosts or \
            any(a.startswith("*.") and (host == a[2:] or host.endswith(a[1:])) for a in allow_hosts):
        return True, "explicitly allowed"
    if parts.username or parts.password:
        return False, "credentials in URL are not allowed"
    if host == "localhost" or host.endswith(_BAD_SUFFIXES):
        return False, f"local host name '{host}' is not allowed"
    try:                                                   # literal IP
        ipaddress.ip_address(host.strip("[]"))
        literal = [host.strip("[]")]
    except ValueError:
        literal = None
    addrs = literal or _resolve(host)
    if not addrs:
        return False, f"could not resolve '{host}'"
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        return False, f"'{host}' points to a non-public address ({bad[0]})"
    return True, "ok"
