"""Stdlib-only generation of first-run credentials."""

import base64
import hashlib
import secrets

HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 240_000
HASH_SEPARATOR = ":"
SALT_BYTES = 16
PASSWORD_BYTES = 18
FERNET_KEY_BYTES = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    return HASH_SEPARATOR.join(
        (
            HASH_SCHEME,
            str(HASH_ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(FERNET_KEY_BYTES)).decode()


def password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)
