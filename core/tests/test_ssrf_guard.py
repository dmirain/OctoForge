"""Tests for the SSRF guard with a stubbed host resolver."""

import pytest

from octoforge_core.net.errors import SsrfBlockedError
from octoforge_core.net.guard import SsrfGuard

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "8.8.8.8"
PRIVATE_IP = "10.0.0.1"
CLOUD_METADATA_IP = "169.254.169.254"
TARGET_URL = "https://api.example.com/data"
RESOLVED_HOST = "api.example.com"


class StubResolver:
    """HostResolver returning a scripted set of addresses."""

    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips
        self.calls: list[str] = []

    async def resolve(self, host: str) -> tuple[str, ...]:
        self.calls.append(host)
        return self._ips


class FailingResolver:
    """HostResolver that fails like an unresolvable host does."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        raise OSError(f"name or service not known: {host}")


def make_guard(ips: tuple[str, ...] = (PUBLIC_IP,)) -> SsrfGuard:
    return SsrfGuard(resolver=StubResolver(ips))


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "10.0.0.1",  # RFC1918 private
        "172.16.0.1",  # RFC1918 private
        "192.168.1.1",  # RFC1918 private
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local cloud metadata endpoint
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "100.64.0.1",  # CGNAT, not globally routable
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local
    ],
)
async def test_non_public_addresses_are_blocked(blocked_ip: str) -> None:
    guard = make_guard((blocked_ip,))

    with pytest.raises(SsrfBlockedError):
        await guard.check(TARGET_URL)


@pytest.mark.parametrize("public_ip", [PUBLIC_IP, OTHER_PUBLIC_IP, "2001:4860:4860::8888"])
async def test_public_addresses_are_allowed(public_ip: str) -> None:
    resolver = StubResolver((public_ip,))
    guard = SsrfGuard(resolver=resolver)

    await guard.check(TARGET_URL)

    assert resolver.calls == [RESOLVED_HOST]


async def test_any_non_public_address_among_many_is_blocked() -> None:
    guard = make_guard((PUBLIC_IP, PRIVATE_IP))

    with pytest.raises(SsrfBlockedError):
        await guard.check(TARGET_URL)


async def test_unresolvable_host_is_blocked() -> None:
    guard = SsrfGuard(resolver=FailingResolver())

    with pytest.raises(SsrfBlockedError):
        await guard.check(TARGET_URL)


async def test_empty_resolution_is_blocked() -> None:
    guard = make_guard(())

    with pytest.raises(SsrfBlockedError):
        await guard.check(TARGET_URL)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.com/data",  # non-http(s) scheme
        "api.example.com/data",  # no scheme at all
        "https://",  # no host
    ],
)
async def test_malformed_or_foreign_scheme_urls_are_blocked_before_resolving(url: str) -> None:
    resolver = StubResolver((PUBLIC_IP,))
    guard = SsrfGuard(resolver=resolver)

    with pytest.raises(SsrfBlockedError):
        await guard.check(url)

    assert resolver.calls == []


async def test_ip_literal_host_is_checked() -> None:
    guard = make_guard((CLOUD_METADATA_IP,))

    with pytest.raises(SsrfBlockedError):
        await guard.check(f"http://{CLOUD_METADATA_IP}/latest/meta-data")


SELF_BASE_URL = "http://127.0.0.1:8000"


async def test_allowed_prefix_skips_resolution() -> None:
    resolver = StubResolver((PRIVATE_IP,))
    guard = SsrfGuard(resolver=resolver, allowed_prefixes=(SELF_BASE_URL,))

    await guard.check(f"{SELF_BASE_URL}/api/cron/jobs")  # loopback, but allowlisted

    assert resolver.calls == []


async def test_non_prefixed_urls_are_still_checked() -> None:
    resolver = StubResolver((PRIVATE_IP,))
    guard = SsrfGuard(resolver=resolver, allowed_prefixes=(SELF_BASE_URL,))

    with pytest.raises(SsrfBlockedError):
        await guard.check(TARGET_URL)

    assert resolver.calls == [RESOLVED_HOST]


async def test_userinfo_spoofed_allowed_origin_is_not_allowlisted() -> None:
    resolver = StubResolver((CLOUD_METADATA_IP,))
    guard = SsrfGuard(resolver=resolver, allowed_prefixes=(SELF_BASE_URL,))

    with pytest.raises(SsrfBlockedError):
        # a raw prefix match would skip the check; the real host is 169.254.169.254
        await guard.check(f"{SELF_BASE_URL}@{CLOUD_METADATA_IP}/latest/meta-data")

    assert resolver.calls == [CLOUD_METADATA_IP]


async def test_lookalike_prefix_host_is_not_allowlisted() -> None:
    resolver = StubResolver((PRIVATE_IP,))
    guard = SsrfGuard(resolver=resolver, allowed_prefixes=(SELF_BASE_URL,))

    with pytest.raises(SsrfBlockedError):
        await guard.check("http://127.0.0.1.evil.com/data")

    assert resolver.calls == ["127.0.0.1.evil.com"]


async def test_different_port_on_the_allowed_host_is_not_allowlisted() -> None:
    resolver = StubResolver((PRIVATE_IP,))
    guard = SsrfGuard(resolver=resolver, allowed_prefixes=(SELF_BASE_URL,))

    with pytest.raises(SsrfBlockedError):
        await guard.check("http://127.0.0.1:9000/admin")


def test_invalid_allowed_prefix_fails_fast() -> None:
    with pytest.raises(ValueError, match="allowed prefix"):
        SsrfGuard(allowed_prefixes=("not-a-url",))
