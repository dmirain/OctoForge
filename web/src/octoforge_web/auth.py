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

This is deliberately not a user system: it authenticates *the operator*, not the
agent's users. `X-User-Id` keeps selecting which dialog the chat UI talks to.
"""

import base64
import hashlib
import hmac
import logging
import secrets
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
OPEN_PATHS = frozenset({"/health", "/health/ready", "/secrets.html"})
# The self-service secrets surface authenticates with its own one-time token
# (see api/secrets.py): dialog users have no operator credential, so these
# endpoints must be reachable without Basic auth.
OPEN_PREFIXES = ("/api/secrets/",)


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
    """Check a password against a stored hash; a malformed hash never passes."""
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


def check_basic_auth(request: Request, username: str, password_hash: str) -> None:
    """Authenticate the request or raise 401 with a Basic challenge.

    A missing configuration fails closed with 503: an operator console with an
    empty password would otherwise be reachable from the internet.
    """
    if not username or not password_hash:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=MISCONFIGURED_MESSAGE
        )
    header = request.headers.get("authorization", "")
    if not header.lower().startswith(BASIC_PREFIX):
        raise _unauthorized()
    try:
        decoded = base64.b64decode(header[len(BASIC_PREFIX) :].strip()).decode()
        candidate_user, _, candidate_password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        raise _unauthorized() from None
    user_ok = hmac.compare_digest(candidate_user, username)
    # both checks always run: skipping the hash on a wrong user would leak
    # which half failed through the response time
    password_ok = verify_password(candidate_password, password_hash)
    if not (user_ok and password_ok):
        logger.warning("failed admin login for %r from %s", candidate_user, _client(request))
        raise _unauthorized()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=UNAUTHORIZED_MESSAGE,
        headers=dict(WWW_AUTHENTICATE),
    )


def _client(request: Request) -> str:
    return "unknown" if request.client is None else request.client.host
