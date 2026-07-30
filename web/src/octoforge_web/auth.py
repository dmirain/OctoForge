"""HTTP Basic authentication for the operator console and the dialog API.

Until now every HTTP endpoint trusted an `X-User-Id` header, which is fine on a
loopback deployment and indefensible the moment the host answers on a public
name: anyone could pass another user's id and read their dialogs. So the whole
surface (everything except the health probes, which the container healthcheck
and any uptime monitor need unauthenticated) sits behind one credential.

The password is stored as a PBKDF2-HMAC-SHA256 hash
(`pbkdf2_sha256:iterations:salt:digest`), verified in constant time.
Hashing is stdlib (`hashlib.pbkdf2_hmac`) rather than passlib/bcrypt on purpose:
one operator credential does not justify a new dependency, and PBKDF2 with a
six-figure iteration count is the right tool for a high-entropy generated
secret. Generate one with `tools/hash_password.py`.

That iteration count is also a weapon pointed at the server: one verification
costs ~60 ms of CPU, and this process serves every dialog on one event loop, so
an unauthenticated flood of wrong passwords used to freeze the whole
installation (~17 requests per second saturated a core). Three things keep that
from happening, in this order:

1. `AttemptLimiter` — a per-client budget of failures. Once a client has burned
   it, the next attempts are refused for a cooldown *without* hashing anything.
2. `verify_password` runs in a worker thread (`authenticate`), so even the
   attempts that do hash never block the loop.
3. `CredentialCache` — a successful credential is remembered for a short while,
   so the console's normal traffic pays the hash once rather than per request.

This is deliberately not a user system: it authenticates *the operator*, not the
agent's users. `X-User-Id` keeps selecting which dialog the chat UI talks to.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 240_000
# Not the conventional "$" of PHC strings: docker compose interpolates "$" in
# .env, and a hash written there arrives truncated in the container (its
# segments are read as unset variable references). ":" cannot appear in the
# base64 alphabet, so it separates unambiguously and survives every consumer.
HASH_SEPARATOR = ":"
SALT_BYTES = 16
WWW_AUTHENTICATE = {"WWW-Authenticate": 'Basic realm="OctoForge"'}
BASIC_PREFIX = "basic "
UNAUTHORIZED_MESSAGE = "authentication required"
MISCONFIGURED_MESSAGE = "admin credentials are not configured"
TOO_MANY_ATTEMPTS_MESSAGE = "too many failed attempts; try again later"
OPEN_PATHS = frozenset({"/health", "/health/ready", "/secrets.html"})
# The self-service secrets surface authenticates with its own one-time token
# (see api/secrets.py): dialog users have no operator credential, so these
# endpoints must be reachable without Basic auth.
OPEN_PREFIXES = ("/api/secrets/",)

# Cross-site request forgery, in the shape Basic auth allows: a browser holding
# the operator credential attaches it to every request to this origin, including
# one an attacker's page submits — so a form on any site could publish a record
# or disable a cron job as the operator.
#
# The check reads the browser's own account of where the request came from
# (`Sec-Fetch-Site`, sent by every current browser, falling back to `Origin`)
# and refuses state-changing requests that did not originate here. Requests with
# neither header are not from a browser — curl, the agent's own `external_call`,
# a deployment script — and are left alone: they carry no ambient credential to
# abuse, and demanding a custom header from them would break every API client.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FETCH_SITE_HEADER = "sec-fetch-site"
ORIGIN_HEADER = "origin"
SAME_SITE_VALUES = frozenset({"same-origin", "none"})
CROSS_SITE_MESSAGE = "cross-site state-changing requests are refused"

# Failure budget per client before the cooldown starts, and how long that
# cooldown lasts. Generous enough that a human retyping a password never meets
# it, tight enough that a flood costs the server nothing after five tries.
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_COOLDOWN_SECONDS = 60.0
# Bounded so a spray from many addresses cannot grow memory without limit.
MAX_TRACKED_CLIENTS = 10_000
# How long a verified credential stays accepted without re-hashing. Short
# enough that a rotated password takes effect within a minute.
CREDENTIAL_CACHE_SECONDS = 60.0
MAX_CACHED_CREDENTIALS = 32
UNKNOWN_CLIENT = "unknown"


def hash_password(password: str, *, iterations: int = HASH_ITERATIONS) -> str:
    """Return a `scheme:iterations:salt:digest` string for `OF_ADMIN_PASSWORD_HASH`."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return HASH_SEPARATOR.join(
        (
            HASH_SCHEME,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash; a malformed hash never passes.

    Costs ~60 ms by design. Call it through `authenticate`, never inline on the
    event loop.
    """
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split(HASH_SEPARATOR)
        if scheme != HASH_SCHEME:
            return False
        expected = base64.b64decode(raw_salt), base64.b64decode(raw_digest)
        iterations = int(raw_iterations)
    except (ValueError, TypeError):
        logger.warning("admin password hash is malformed; refusing every login")
        return False
    salt, digest = expected
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(candidate, digest)


def is_open_path(path: str) -> bool:
    """Whether the path is served without the operator credential.

    Health probes plus the token-authenticated self-service secrets surface.
    """
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def is_cross_site_mutation(request: Request) -> bool:
    """Whether a browser is submitting a state-changing request from another site."""
    if request.method.upper() not in MUTATING_METHODS:
        return False
    fetch_site = request.headers.get(FETCH_SITE_HEADER, "").lower()
    if fetch_site:
        return fetch_site not in SAME_SITE_VALUES
    origin = request.headers.get(ORIGIN_HEADER)
    if not origin:
        return False  # not a browser: no ambient credential to forge with
    return origin.rstrip("/").lower() != _own_origin(request)


def _own_origin(request: Request) -> str:
    """The origin this request was addressed to, as a browser would spell it."""
    url = request.url
    host = request.headers.get("host") or url.netloc
    return f"{url.scheme}://{host}".rstrip("/").lower()


@dataclass(slots=True)
class AttemptLimiter:
    """Per-client failure budget: refuses further attempts without hashing.

    Keyed by client address. The point is not to stop a determined attacker
    from guessing (a generated password has far too much entropy for that) but
    to make failing cheap for the server: past the budget an attempt costs a
    dict lookup instead of a PBKDF2 run.
    """

    max_failures: int = MAX_FAILED_ATTEMPTS
    cooldown_seconds: float = ATTEMPT_COOLDOWN_SECONDS
    max_clients: int = MAX_TRACKED_CLIENTS
    _failures: OrderedDict[str, tuple[int, float]] = field(default_factory=OrderedDict)

    def blocked(self, client: str, now: float | None = None) -> bool:
        """Whether this client is inside its cooldown right now."""
        moment = time.monotonic() if now is None else now
        entry = self._failures.get(client)
        if entry is None:
            return False
        count, last = entry
        if moment - last >= self.cooldown_seconds:
            del self._failures[client]
            return False
        return count >= self.max_failures

    def record_failure(self, client: str, now: float | None = None) -> None:
        """Count a failed attempt, starting or extending the cooldown."""
        moment = time.monotonic() if now is None else now
        count, last = self._failures.get(client, (0, moment))
        if moment - last >= self.cooldown_seconds:
            count = 0
        self._failures[client] = (count + 1, moment)
        self._failures.move_to_end(client)
        while len(self._failures) > self.max_clients:
            self._failures.popitem(last=False)

    def record_success(self, client: str) -> None:
        """Forget a client's failures once it proves it knows the password."""
        self._failures.pop(client, None)


@dataclass(slots=True)
class CredentialCache:
    """Remembers verified credentials briefly so hashing is not per-request.

    The key is a digest of what the client presented, so a wrong password can
    never hit an entry created by the right one. Only successes are stored:
    caching failures would let a flood fill the map.
    """

    ttl_seconds: float = CREDENTIAL_CACHE_SECONDS
    max_entries: int = MAX_CACHED_CREDENTIALS
    _entries: OrderedDict[str, float] = field(default_factory=OrderedDict)

    def valid(self, header: str, now: float | None = None) -> bool:
        """Whether this exact credential was verified within the TTL."""
        moment = time.monotonic() if now is None else now
        key = _credential_key(header)
        expires_at = self._entries.get(key)
        if expires_at is None:
            return False
        if expires_at <= moment:
            del self._entries[key]
            return False
        return True

    def remember(self, header: str, now: float | None = None) -> None:
        """Accept this credential without hashing until the TTL runs out."""
        moment = time.monotonic() if now is None else now
        self._entries[_credential_key(header)] = moment + self.ttl_seconds
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every remembered credential (configuration changed)."""
        self._entries.clear()


@dataclass(frozen=True, slots=True)
class AuthGate:
    """The operator gate: a limiter, a cache and the credential to check against."""

    username: str
    password_hash: str
    limiter: AttemptLimiter = field(default_factory=AttemptLimiter)
    cache: CredentialCache = field(default_factory=CredentialCache)

    async def authenticate(self, request: Request) -> None:
        """Authenticate the request or raise; hashing happens off the event loop.

        A missing configuration fails closed with 503: an operator console with
        an empty password would otherwise be reachable from the internet.
        """
        if not self.username or not self.password_hash:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=MISCONFIGURED_MESSAGE
            )
        header = request.headers.get("authorization", "")
        if not header.lower().startswith(BASIC_PREFIX):
            raise _unauthorized()
        if self.cache.valid(header):
            return
        client = _client(request)
        if self.limiter.blocked(client):
            logger.warning("admin login refused (cooldown) from %s", client)
            raise HTTPException(
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
                detail=TOO_MANY_ATTEMPTS_MESSAGE,
                headers=dict(WWW_AUTHENTICATE),
            )
        candidate = _decode_basic(header)
        if candidate is None:
            self.limiter.record_failure(client)
            raise _unauthorized()
        candidate_user, candidate_password = candidate
        user_ok = hmac.compare_digest(candidate_user, self.username)
        # PBKDF2 is ~60 ms of CPU: on the loop it would stall every dialog in
        # the process, so it runs in a worker thread. Both checks always run —
        # skipping the hash on a wrong user would leak which half failed.
        password_ok = await asyncio.to_thread(
            verify_password, candidate_password, self.password_hash
        )
        if not (user_ok and password_ok):
            self.limiter.record_failure(client)
            logger.warning("failed admin login for %r from %s", candidate_user, client)
            raise _unauthorized()
        self.limiter.record_success(client)
        self.cache.remember(header)


def _decode_basic(header: str) -> tuple[str, str] | None:
    """Split a Basic header into (user, password); None when it does not decode."""
    try:
        decoded = base64.b64decode(header[len(BASIC_PREFIX) :].strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    user, _, password = decoded.partition(":")
    return user, password


def _credential_key(header: str) -> str:
    """Digest of a credential header: the cache never stores the credential itself."""
    return hashlib.sha256(header.encode()).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=UNAUTHORIZED_MESSAGE,
        headers=dict(WWW_AUTHENTICATE),
    )


def _client(request: Request) -> str:
    return UNKNOWN_CLIENT if request.client is None else request.client.host
