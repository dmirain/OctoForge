"""Secret values, metadata and errors shared across the module boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SecretNotFoundError(Exception):
    """Raised when no secret matches the requested user and code."""


class SecretHostMismatchError(Exception):
    """Raised when a secret is requested for a host outside its binding."""


class InvalidSecretError(Exception):
    """Raised when a secret or its metadata is malformed."""


class SecretPlacement(StrEnum):
    """A request part a secret value may be substituted into."""

    HEADER = "header"
    URL = "url"
    BODY = "body"


DEFAULT_PLACEMENTS: frozenset[SecretPlacement] = frozenset({SecretPlacement.HEADER})


class SecretTransform(StrEnum):
    """A static transform applied to a stored value before substitution."""

    BASE64 = "base64"
    BASE64URL = "base64url"
    MD5_HEX = "md5_hex"
    SHA1_HEX = "sha1_hex"
    SHA256_HEX = "sha256_hex"


@dataclass(frozen=True, slots=True)
class SecretWrite:
    """Complete input for storing or replacing one secret."""

    user_id: str
    code: str
    value: str
    allowed_host: str
    description: str
    placements: tuple[str, ...] = ()
    transform: str | None = None


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """Metadata of one stored secret; the value is intentionally absent."""

    code: str
    allowed_host: str
    description: str
    placements: frozenset[SecretPlacement]
    transform: SecretTransform | None
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """Transformed and plain forms needed for one request and response scrubbing."""

    value: str
    plain: str
    placements: frozenset[SecretPlacement]
