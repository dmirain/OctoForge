"""Validation and normalization policy for secret metadata."""

import re
from collections.abc import Iterable

from octoforge_core.secrets.types import (
    DEFAULT_PLACEMENTS,
    InvalidSecretError,
    SecretPlacement,
    SecretTransform,
)

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_DESCRIPTION_CHARS = 256
WILDCARD_PREFIX = "*."
MIN_PATTERN_SUFFIX_DOTS = 1


def normalize_code(raw: str) -> str:
    """Validate and normalize a snake_case secret code."""
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidSecretError(
            "secret code must be 1-64 characters of [a-z0-9_], e.g. 'gmail_token'"
        )
    return code


def normalize_host(raw: str) -> str:
    """Validate a hostname or narrow, one-label wildcard binding."""
    host = raw.strip().lower().rstrip(".")
    if not host or any(character in host for character in "/: "):
        raise InvalidSecretError(
            "allowed_host must be a bare hostname ('api.example.com') or a "
            "one-level pattern ('*.example.com')"
        )
    if not host.startswith(WILDCARD_PREFIX):
        if "*" in host:
            raise InvalidSecretError(
                "a wildcard is only allowed as the leading label, e.g. '*.example.com'"
            )
        return host
    suffix = host[len(WILDCARD_PREFIX) :]
    if "*" in suffix:
        raise InvalidSecretError("only one wildcard label is allowed, e.g. '*.example.com'")
    if suffix.count(".") < MIN_PATTERN_SUFFIX_DOTS or "" in suffix.split("."):
        raise InvalidSecretError(
            f"'{host}' is too broad: a pattern must name at least a domain and a "
            "suffix, e.g. '*.example.com'"
        )
    return host


def host_matches(binding: str, host: str) -> bool:
    """Whether a request host is covered by an exact or one-label binding."""
    target = host.strip().lower().rstrip(".")
    if "*" in target:
        return False
    if not binding.startswith(WILDCARD_PREFIX):
        return binding == target
    suffix = binding[len(WILDCARD_PREFIX) :]
    if not target.endswith(f".{suffix}"):
        return False
    label = target[: -(len(suffix) + 1)]
    return bool(label) and "." not in label


def normalize_description(raw: str) -> str:
    """Validate the required human-facing purpose of a secret."""
    description = " ".join(raw.split())
    if not description:
        raise InvalidSecretError(
            "description is required: say what the secret is for, e.g. "
            "'read-only token for the work calendar'"
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise InvalidSecretError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters")
    return description


def normalize_placements(raw: Iterable[str]) -> frozenset[SecretPlacement]:
    """Validate placements; empty means the header-only default."""
    placements = set()
    for item in raw:
        try:
            placements.add(SecretPlacement(str(item).strip().lower()))
        except ValueError:
            allowed = ", ".join(member.value for member in SecretPlacement)
            raise InvalidSecretError(f"unknown placement {item!r}; allowed: {allowed}") from None
    return frozenset(placements) if placements else DEFAULT_PLACEMENTS


def normalize_transform(raw: str | None) -> SecretTransform | None:
    """Validate a transform selection; empty means no transform."""
    if raw is None or not str(raw).strip():
        return None
    try:
        return SecretTransform(str(raw).strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in SecretTransform)
        raise InvalidSecretError(f"unknown transform {raw!r}; allowed: {allowed}") from None
