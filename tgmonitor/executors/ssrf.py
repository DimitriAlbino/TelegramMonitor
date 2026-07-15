"""SSRF destination guard (#22).

Monitor targets are user-supplied URLs/host:ports that the server fetches
directly. This module refuses internal destinations — loopback, link-local
(including the cloud-metadata 169.254.169.254), RFC1918, CGNAT, ULA,
IPv6 link-local, multicast, and other non-global ranges — whether given as an
IP literal or a hostname that resolves to one.

Two entry points:

- :func:`is_blocked_ip` — pure check on an :class:`ipaddress.ip_address`.
- :func:`assert_safe_destination` — parses a URL or ``host:port`` target,
  resolves the host via an injected resolver (so tests fake DNS), and raises
  :class:`DestinationBlocked` if any resolved address is internal.

The HTTP transport consults this before the request *and* re-checks each
redirect target (an external URL that 302s to an internal one is a classic
SSRF). TCP consults it before ``open_connection``.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


class DestinationBlocked(Exception):
    """Raised when a target resolves to a blocked (internal) address."""


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is internal/non-global and must not be probed.

    Blocks loopback, private (RFC1918), link-local (incl. 169.254 cloud
    metadata), CGNAT (100.64/10), ULA (fc00::/7), IPv6 link-local, multicast,
    unspecified, and reserved ranges. A global unicast address is allowed.
    """
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    # CGNAT 100.64.0.0/10 (is_private does not always cover it across versions).
    return isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("100.64.0.0/10")


# A resolver maps a hostname to a list of address strings (A/AAAA). The default
# uses real DNS; tests inject a fake. Returns [] on resolution failure.
Resolver = Callable[[str], list[str]]


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return list({str(info[4][0]) for info in infos})


def assert_safe_destination(
    target: str,
    *,
    resolver: Resolver | None = None,
    is_host_port: bool = False,
) -> None:
    """Resolve ``target`` and raise :class:`DestinationBlocked` if internal.

    ``target`` is a URL (``http(s)://host[/...]``) by default; pass
    ``is_host_port=True`` for TCP targets of the form ``host:port``.

    IP literals are checked directly (no DNS). Hostnames are resolved via
    ``resolver`` (real DNS by default); if *any* resolved address is blocked the
    target is refused (defensive: a round-robin name with one internal address
    could otherwise reach the internal hop).
    """
    resolve = resolver or _default_resolver
    host = _extract_host(target, is_host_port=is_host_port)
    if not host:
        raise DestinationBlocked(f"could not parse host from target {target!r}")

    # IP literal? Check directly without DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if is_blocked_ip(ip):
            raise DestinationBlocked(f"target IP {ip} is a blocked internal address")
        return

    addrs = resolve(host)
    if not addrs:
        # DNS failure is surfaced as a normal probe failure by the executor
        # (connection failed), not as a block — but an unresolvable host can't
        # be internal, so allow it through to fail at connect time.
        return
    for addr in addrs:
        # IPv6 literals from getaddrinfo may carry a zone id (fe80::1%eth0);
        # strip it for parsing.
        addr = addr.split("%", 1)[0]
        try:
            ipobj = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if is_blocked_ip(ipobj):
            raise DestinationBlocked(f"host {host!r} resolves to blocked internal address {addr}")


def pick_safe_ip(host: str, *, resolver: Resolver | None = None) -> str | None:
    """Resolve ``host`` and return one safe IP to pin a connection to (#39).

    Raises :class:`DestinationBlocked` if *any* resolved address is internal
    (defensive against a round-robin name with one internal answer). Returns
    ``None`` if the host does not resolve — the caller then connects by name and
    fails naturally at connect time. Pinning to the returned IP closes the
    DNS-rebinding TOCTOU: the address validated here is the exact one connected
    to, so a name cannot answer public to the guard and internal to the client.
    """
    resolve = resolver or _default_resolver
    addrs = resolve(host)
    if not addrs:
        return None
    safe: list[str] = []
    for addr in addrs:
        a = addr.split("%", 1)[0]  # strip an IPv6 zone id (fe80::1%eth0)
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if is_blocked_ip(ip):
            raise DestinationBlocked(f"host {host!r} resolves to blocked internal address {a}")
        safe.append(a)
    return safe[0] if safe else None


def _extract_host(target: str, *, is_host_port: bool) -> str:
    """Pull the hostname out of a URL or ``host:port`` string.

    For IPv6 ``host:port`` this is ambiguous; the TCP executor splits before
    calling here, so we only handle the common host:port and URL forms.
    """
    if is_host_port:
        # Strip the port. Handles host:port; for [ipv6]:port the bracketed form
        # is preserved by the caller splitting it out first.
        if target.startswith("["):
            # [ipv6]:port
            end = target.find("]")
            return target[1:end] if end != -1 else target[1:]
        host, _, _port = target.rpartition(":")
        return host
    parsed = urlparse(target)
    return parsed.hostname or ""
