"""SSRF guard: reject outbound URLs that resolve to non-public addresses.

The hostname is resolved and EVERY resolved address is checked with the
`ipaddress` predicates: private, loopback, link-local (covers the cloud
metadata address 169.254.169.254), multicast, reserved and unspecified are
blocked, as is any non-globally-routable address (e.g. the CGNAT range
100.64.0.0/10, which passes all six named predicates).

Known limitation (TOCTOU / DNS rebinding): the address is resolved at check
time, but the HTTP client resolves the hostname again when connecting, and an
attacker-controlled DNS record can answer differently between the two lookups.
Closing that gap requires connecting by resolved IP with the Host header set —
out of scope for this stage.
"""

import asyncio
import ipaddress
import socket
from typing import Protocol
from urllib.parse import urlsplit

from octoforge_core.net.errors import SsrfBlockedError

ALLOWED_SCHEMES = ("http", "https")
GETADDRINFO_PORT = 0


class HostResolver(Protocol):
    """Resolves a hostname into IP address strings (injectable for tests)."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        """Return every IP address the host resolves to."""
        ...


class SystemResolver:
    """HostResolver over the OS resolver, run off the event loop's thread."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, GETADDRINFO_PORT, type=socket.SOCK_STREAM)
        return tuple({str(info[4][0]) for info in infos})


class SsrfGuard:
    """Checks outbound URLs before the HTTP client is allowed to follow them."""

    def __init__(self, resolver: HostResolver | None = None) -> None:
        self._resolver = resolver if resolver is not None else SystemResolver()

    async def check(self, url: str) -> None:
        """Raise SsrfBlockedError unless the URL is http(s) and publicly routed."""
        host = self._public_host(url)
        try:
            addresses = await self._resolver.resolve(host)
        except OSError as exc:
            raise SsrfBlockedError(f"host does not resolve: {host}") from exc
        if not addresses:
            raise SsrfBlockedError(f"host does not resolve: {host}")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if _is_blocked(ip):
                raise SsrfBlockedError(f"url resolves to a non-public address: {url}")

    @staticmethod
    def _public_host(url: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise SsrfBlockedError(f"unparseable url: {url}") from exc
        if parts.scheme not in ALLOWED_SCHEMES:
            raise SsrfBlockedError(f"url scheme is not http(s): {url}")
        if not parts.hostname:
            raise SsrfBlockedError(f"url has no host: {url}")
        return parts.hostname


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast  # global in the IANA registry, still not a fetch target
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global  # CGNAT 100.64.0.0/10 and any future non-public space
    )
