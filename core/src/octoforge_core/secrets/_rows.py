"""Mapping and compact column encoding for persisted secret metadata."""

from octoforge_core.secrets.models import SecretRow
from octoforge_core.secrets.types import (
    DEFAULT_PLACEMENTS,
    InvalidSecretError,
    SecretInfo,
    SecretPlacement,
    SecretTransform,
)


def to_info(row: SecretRow) -> SecretInfo:
    return SecretInfo(
        code=row.code,
        allowed_host=row.allowed_host,
        description=row.description,
        placements=placements_from_column(row.placements),
        transform=transform_from_column(row.transform),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def placements_to_column(placements: frozenset[SecretPlacement]) -> str | None:
    if placements == DEFAULT_PLACEMENTS:
        return None
    return ",".join(sorted(member.value for member in placements))


def placements_from_column(raw: str | None) -> frozenset[SecretPlacement]:
    """Unknown persisted placements degrade to the safe header-only default."""
    if raw is None:
        return DEFAULT_PLACEMENTS
    placements = set()
    for item in raw.split(","):
        try:
            placements.add(SecretPlacement(item))
        except ValueError:
            continue
    return frozenset(placements) if placements else DEFAULT_PLACEMENTS


def transform_from_column(raw: str | None) -> SecretTransform | None:
    """Reject an unknown transform rather than substitute the plain value."""
    if raw is None:
        return None
    try:
        return SecretTransform(raw)
    except ValueError:
        raise InvalidSecretError(f"stored transform {raw!r} is not supported") from None
