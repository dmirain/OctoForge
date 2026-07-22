"""SSRF guard: reject outbound URLs that resolve to non-public addresses.

The hostname is resolved and EVERY resolved address is checked with the
`ipaddress` predicates: private, loopback, link-local (covers the cloud
metadata address 169.254.169.254), multicast, reserved and unspecified are
blocked, as is any non-globally-routable address (e.g. the CGNAT range
100.64.0.0/10, which passes all six named predicates).

`allowed_prefixes` bypass the resolve-and-check pipeline by ORIGIN: a URL
whose (scheme, host, port) — the port defaulted by the scheme — matches an
allowlisted prefix's origin skips the check. The comparison is parsed, never
a raw string prefix match, so userinfo like `http://allowed@169.254.169.254/`
does NOT match (the real host is compared) and the prefix's path is ignored
(an allowlisted origin trusts every path on it). Reserve the allowlist for
the composition root's own base URL (the application calling its own HTTP
API, which may sit on a loopback address) — never for user-controlled targets.

Known limitation (TOCTOU / DNS rebinding): the address is resolved at check
time, but the HTTP client resolves the hostname again when connecting, and an
attacker-controlled DNS record can answer differently between the two lookups.
Closing that gap requires connecting by resolved IP with the Host header set —
out of scope for this stage.
"""

import asyncio
import ipaddress
import socket
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit

from octoforge_core.net.errors import SsrfBlockedError

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}
GETADDRINFO_PORT = 0


class UrlOrigin(NamedTuple):
    """Parsed scheme/host/port triple of a URL (the port defaulted by the scheme)."""

    scheme: str
    host: str
    port: int


def parse_origin(url: str) -> UrlOrigin | None:
    """Parse an http(s) URL into its origin; None when it is not a valid http(s) URL."""
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        return None
    return UrlOrigin(
        scheme=parts.scheme,
        host=parts.hostname,
        port=port if port is not None else DEFAULT_PORTS[parts.scheme],
    )


def matches_url_prefix(url: str, prefix: str) -> bool:
    """Whether `url` and `prefix` share the same origin (parsed, userinfo-safe)."""
    url_origin = parse_origin(url)
    return url_origin is not None and url_origin == parse_origin(prefix)


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
    """Checks outbound URLs before the HTTP client is allowed to follow them.

    `allowed_prefixes` skip the resolve-and-check pipeline for URLs sharing
    their origin (see the module docstring). An invalid prefix is rejected at
    construction so a composition-root misconfiguration fails fast.
    """

    def __init__(
        self,
        resolver: HostResolver | None = None,
        allowed_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._resolver = resolver if resolver is not None else SystemResolver()
        self._allowed_origins = tuple(
            self._allowlisted_origin(prefix) for prefix in allowed_prefixes
        )

    @staticmethod
    def _allowlisted_origin(prefix: str) -> UrlOrigin:
        origin = parse_origin(prefix)
        if origin is None:
            raise ValueError(f"allowed prefix is not an http(s) origin: {prefix!r}")
        return origin

    async def check(self, url: str) -> None:
        """Raise SsrfBlockedError unless the URL is http(s) and publicly routed."""
        origin = parse_origin(url)
        if origin is None:
            raise SsrfBlockedError(f"url is not a valid http(s) url: {url}")
        if origin in self._allowed_origins:
            return
        try:
            addresses = await self._resolver.resolve(origin.host)
        except OSError as exc:
            raise SsrfBlockedError(f"host does not resolve: {origin.host}") from exc
        if not addresses:
            raise SsrfBlockedError(f"host does not resolve: {origin.host}")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if _is_blocked(ip):
                raise SsrfBlockedError(f"url resolves to a non-public address: {url}")


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
