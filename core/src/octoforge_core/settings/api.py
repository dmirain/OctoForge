"""Public boundary of the settings module.

Installation-wide knobs an operator turns at runtime: a generic key→value
table edited from the console, so changing one never needs a redeploy — the
reason these are not environment variables. Environment stays for what wires
the process together (URLs, credentials, ports); this table holds what
governs behavior while the process runs.

Keys share the params/secrets grammar (`^[a-z0-9_]{1,64}$`). An absent key
is not an error but "the installation never set this" — every reader must
carry a meaning for absence, the way `max_active_users` reads it as
"no cap".
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_VALUE_CHARS = 2048

#: How many users may hold `active` status at once. Absent/empty = no cap
#: (everyone activates on first contact); `0` = no free slots at all — the
#: operator's pause button on registration.
MAX_ACTIVE_USERS_KEY = "max_active_users"


class InvalidSettingError(Exception):
    """Raised when a setting being stored is malformed (key or value)."""


@dataclass(frozen=True, slots=True)
class Setting:
    """One stored installation setting."""

    key: str
    value: str
    updated_at: datetime


def normalize_key(raw: str) -> str:
    """Validate and normalize a setting key (snake_case, ≤64 chars)."""
    key = raw.strip().lower()
    if not KEY_PATTERN.match(key):
        raise InvalidSettingError(
            "setting key must be 1-64 characters of [a-z0-9_], e.g. 'max_active_users'"
        )
    return key


def normalize_value(raw: str) -> str:
    """Validate a setting value: non-empty, bounded (deleting clears a key)."""
    value = raw.strip()
    if not value or len(value) > MAX_VALUE_CHARS:
        raise InvalidSettingError(f"setting value must be 1..{MAX_VALUE_CHARS} characters")
    return value


class SettingsStore(Protocol):
    """Port of the settings module."""

    async def get(self, key: str) -> str | None:
        """The stored value, or None when the installation never set the key."""
        ...

    async def put(self, key: str, value: str) -> Setting:
        """Store or replace the value under `key`."""
        ...

    async def delete(self, key: str) -> None:
        """Clear the key; deleting an absent key is a no-op — absence is the goal."""
        ...

    async def list(self) -> list[Setting]:
        """Every stored setting ordered by key (operator console)."""
        ...


async def max_active_users(store: SettingsStore) -> int | None:
    """The active-user cap; None = no cap. A malformed stored value fails open.

    Failing open (no cap) over failing closed (cap 0) on purpose: a corrupt
    row must not silently freeze registration for the whole installation.
    """
    raw = await store.get(MAX_ACTIVE_USERS_KEY)
    if raw is None:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return cap if cap >= 0 else None
