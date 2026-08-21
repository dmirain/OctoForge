"""Values and errors shared by the identity port and its adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class UserStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    BANNED = "banned"


class UserNotFoundError(Exception):
    """A user id does not resolve to a stored user."""


class IdentityNotFoundError(Exception):
    """A surface account is not linked to anyone."""


class IdentityTakenError(Exception):
    """A surface account already belongs to another person."""


@dataclass(frozen=True, slots=True)
class IdentityKey:
    surface: str
    external_id: str


@dataclass(frozen=True, slots=True)
class IdentityLink:
    user_id: str
    key: IdentityKey
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    key: IdentityKey
    name: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str = ""
    email: str = ""
    status: UserStatus = UserStatus.ACTIVE
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: str
    surface: str
    external_id: str
    name: str = ""
    username: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


UserList = list[User]
UserIdentityList = list[UserIdentity]
