"""Public boundary of the secrets module.

Per-user secrets (API tokens and the like) that must never reach the LLM
context, the message archive or the logs. The rest of the system knows only
secret *codes*: an endpoint record declares which code it needs, and the call
executor resolves the value at request time — nothing else ever reads it.
The store DTO deliberately carries no value field: every listing surface
(web form, admin, tools) is metadata-only by construction.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


class SecretNotFoundError(Exception):
    """Raised when no secret matches the requested (user, code) pair."""


class SecretHostMismatchError(Exception):
    """Raised when a secret is asked for a host it is not bound to.

    The host binding is the exfiltration guard: even a poisoned endpoint
    record (or a typo in one) cannot send a secret anywhere but the host it
    was created for.
    """


class InvalidSecretError(Exception):
    """Raised when a secret being stored is malformed (code, value or host)."""


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """Metadata of one stored secret; the value is intentionally absent."""

    code: str
    allowed_host: str
    created_at: datetime
    last_used_at: datetime | None


def normalize_code(raw: str) -> str:
    """Validate and normalize a secret code (snake_case, ≤64 chars)."""
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidSecretError(
            "secret code must be 1-64 characters of [a-z0-9_], e.g. 'gmail_token'"
        )
    return code


def normalize_host(raw: str) -> str:
    """Validate and normalize the host a secret is bound to (exact hostname)."""
    host = raw.strip().lower().rstrip(".")
    if not host or "/" in host or ":" in host or " " in host:
        raise InvalidSecretError("allowed_host must be a bare hostname, e.g. 'api.example.com'")
    return host


class SecretStore(Protocol):
    """Port of the secrets module: encrypted at rest, value readable only via resolve."""

    async def put(self, user_id: str, code: str, value: str, allowed_host: str) -> SecretInfo:
        """Store or replace the user's secret under `code`, bound to `allowed_host`."""
        ...

    async def list(self, user_id: str) -> list[SecretInfo]:
        """Return the user's secrets metadata, newest first. Never the values."""
        ...

    async def delete(self, user_id: str, code: str) -> None:
        """Delete the user's secret by code; raise `SecretNotFoundError`."""
        ...

    async def resolve(self, user_id: str, code: str, host: str) -> str:
        """Return the decrypted value for a request going to `host`.

        The single place a value leaves the store. Raises
        `SecretNotFoundError` for an unknown code and
        `SecretHostMismatchError` when `host` differs from the binding
        (the error text never contains the value). Stamps `last_used_at`.
        """
        ...
