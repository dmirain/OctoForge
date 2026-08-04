"""Public boundary of the user-params module.

Deterministic per-user values a record template references as `{user.code}`:
a timezone, an account id, a calendar path. Not secrets — the operator sees
and edits them in the admin console, listings carry the values — but they
share the substitution discipline: the executor injects them server-side, so
the model can rely on them without carrying them through its context.

Codes share the secrets grammar (`^[a-z0-9_]{1,64}$`) so a template author
never has to remember two naming rules.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_VALUE_CHARS = 2048


class UserParamNotFoundError(Exception):
    """Raised when no param matches the requested (user, code) pair."""


class InvalidUserParamError(Exception):
    """Raised when a param being stored is malformed (code or value)."""


@dataclass(frozen=True, slots=True)
class UserParam:
    """One stored per-user parameter; values are plain data, not secrets."""

    code: str
    value: str
    created_at: datetime
    updated_at: datetime


def normalize_code(raw: str) -> str:
    """Validate and normalize a param code (snake_case, ≤64 chars)."""
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidUserParamError(
            "param code must be 1-64 characters of [a-z0-9_], e.g. 'timezone'"
        )
    return code


def normalize_value(raw: str) -> str:
    """Validate a param value: non-empty, bounded, no control characters.

    UTF-8 text is fine (a value may go into a request body); header-safety
    is checked at substitution time, only where the template actually puts
    the value into a header.
    """
    value = raw.strip()
    if not value or len(value) > MAX_VALUE_CHARS:
        raise InvalidUserParamError(f"param value must be 1..{MAX_VALUE_CHARS} characters")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise InvalidUserParamError("param value must not contain control characters")
    return value


class UserParamStore(Protocol):
    """Port of the user-params module."""

    async def put(self, user_id: str, code: str, value: str) -> UserParam:
        """Store or replace the user's param under `code`."""
        ...

    async def get_for_user(self, user_id: str) -> dict[str, str]:
        """Return every param of the user as a code→value mapping."""
        ...

    async def list(self, user_id: str) -> list[UserParam]:
        """Return the user's params ordered by code."""
        ...

    async def delete(self, user_id: str, code: str) -> None:
        """Delete the user's param by code; raise `UserParamNotFoundError`."""
        ...
