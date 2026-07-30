"""Tests for the operator gate: hashing off the loop, the failure budget, CSRF."""

import base64
import threading
from http import HTTPStatus

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from octoforge_web import auth
from octoforge_web.auth import (
    AttemptLimiter,
    AuthGate,
    CredentialCache,
    hash_password,
    is_cross_site_mutation,
    is_open_path,
)

PASSWORD = "correct-horse-battery-staple"
USERNAME = "admin"
CLIENT = "203.0.113.7"


def basic(user: str, password: str) -> str:
    """Render a Basic authorization header value."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def make_request(headers: dict[str, str], method: str = "GET", path: str = "/api/x") -> Request:
    """Build a Starlette request with the given headers and client address."""
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw,
            "client": (CLIENT, 12345),
            "scheme": "https",
            "server": ("example.org", 443),
            "root_path": "",
        }
    )


@pytest.fixture
def gate() -> AuthGate:
    return AuthGate(username=USERNAME, password_hash=hash_password(PASSWORD))


async def test_verification_never_runs_on_the_event_loop(
    gate: AuthGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PBKDF2 costs ~60 ms; on the loop that stalls every dialog in the process
    threads: list[str] = []
    real = auth.verify_password

    def spy(password: str, encoded: str) -> bool:
        threads.append(threading.current_thread().name)
        return real(password, encoded)

    monkeypatch.setattr(auth, "verify_password", spy)

    await gate.authenticate(make_request({"authorization": basic(USERNAME, PASSWORD)}))

    assert threads and all(name != threading.main_thread().name for name in threads)


async def test_a_verified_credential_is_not_hashed_again(
    gate: AuthGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, e: (calls.append(p), real(p, e))[1])
    request = make_request({"authorization": basic(USERNAME, PASSWORD)})

    await gate.authenticate(request)
    await gate.authenticate(request)
    await gate.authenticate(request)

    assert len(calls) == 1


async def test_a_wrong_password_never_hits_the_cache(
    gate: AuthGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, e: (calls.append(p), real(p, e))[1])
    await gate.authenticate(make_request({"authorization": basic(USERNAME, PASSWORD)}))

    with pytest.raises(HTTPException) as denied:
        await gate.authenticate(make_request({"authorization": basic(USERNAME, "guess")}))

    assert denied.value.status_code == HTTPStatus.UNAUTHORIZED
    assert calls == [PASSWORD, "guess"]


async def test_repeated_failures_stop_costing_a_hash(
    gate: AuthGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, e: (calls.append(p), real(p, e))[1])
    bad = make_request({"authorization": basic(USERNAME, "guess")})

    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        with pytest.raises(HTTPException):
            await gate.authenticate(bad)
    hashed_before_cooldown = len(calls)

    with pytest.raises(HTTPException) as refused:
        await gate.authenticate(bad)

    assert refused.value.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert len(calls) == hashed_before_cooldown  # the flood is now free for us


async def test_the_right_password_clears_the_budget(gate: AuthGate) -> None:
    for _ in range(auth.MAX_FAILED_ATTEMPTS - 1):
        with pytest.raises(HTTPException):
            await gate.authenticate(make_request({"authorization": basic(USERNAME, "guess")}))

    await gate.authenticate(make_request({"authorization": basic(USERNAME, PASSWORD)}))

    assert not gate.limiter.blocked(CLIENT)


async def test_missing_configuration_fails_closed() -> None:
    gate = AuthGate(username=USERNAME, password_hash="")

    with pytest.raises(HTTPException) as denied:
        await gate.authenticate(make_request({"authorization": basic(USERNAME, PASSWORD)}))

    assert denied.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_attempt_limiter_forgets_after_the_cooldown() -> None:
    limiter = AttemptLimiter(max_failures=2, cooldown_seconds=10.0)

    limiter.record_failure(CLIENT, now=100.0)
    limiter.record_failure(CLIENT, now=101.0)

    assert limiter.blocked(CLIENT, now=102.0)
    assert not limiter.blocked(CLIENT, now=120.0)


def test_credential_cache_expires() -> None:
    cache = CredentialCache(ttl_seconds=30.0)
    header = basic(USERNAME, PASSWORD)

    cache.remember(header, now=100.0)

    assert cache.valid(header, now=120.0)
    assert not cache.valid(header, now=140.0)
    assert not cache.valid(basic(USERNAME, "other"), now=120.0)


@pytest.mark.parametrize(
    ("headers", "forged"),
    [
        ({"sec-fetch-site": "cross-site"}, True),
        ({"sec-fetch-site": "same-site"}, True),
        ({"sec-fetch-site": "same-origin"}, False),
        ({"sec-fetch-site": "none"}, False),
        ({"origin": "https://evil.example"}, True),
        ({"origin": "https://example.org", "host": "example.org"}, False),
        ({}, False),  # not a browser: curl, the agent, a deploy script
    ],
)
def test_cross_site_mutations_are_recognized(headers: dict[str, str], forged: bool) -> None:
    request = make_request(headers, method="POST")

    assert is_cross_site_mutation(request) is forged


def test_reads_are_never_treated_as_forgeable() -> None:
    request = make_request({"sec-fetch-site": "cross-site"}, method="GET")

    assert is_cross_site_mutation(request) is False


def test_open_paths_stay_open() -> None:
    assert is_open_path("/health")
    assert is_open_path("/health/ready")
    assert is_open_path("/secrets.html")
    assert is_open_path("/api/secrets/session")
    assert not is_open_path("/api/admin/totals")
    assert not is_open_path("/")


def test_client_helper_survives_a_missing_peer() -> None:
    scope_without_client = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/x",
            "headers": [],
            "query_string": b"",
            "client": None,
            "scheme": "https",
            "server": ("example.org", 443),
        }
    )

    assert auth._client(scope_without_client) == auth.UNKNOWN_CLIENT
