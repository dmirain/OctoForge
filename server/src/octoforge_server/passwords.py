"""PBKDF2 hashing and constant-time verification of operator passwords."""

import base64
import hashlib
import hmac
import logging
import secrets

logger = logging.getLogger(__name__)

HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 240_000
HASH_SEPARATOR = ":"
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = HASH_ITERATIONS) -> str:
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
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split(HASH_SEPARATOR)
        if scheme != HASH_SCHEME:
            return False
        salt = base64.b64decode(raw_salt)
        digest = base64.b64decode(raw_digest)
        iterations = int(raw_iterations)
    except (ValueError, TypeError):
        logger.warning("admin password hash is malformed; refusing every login")
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(candidate, digest)
