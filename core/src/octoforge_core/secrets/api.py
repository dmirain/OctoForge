"""Public boundary of the secrets module."""

from octoforge_core.secrets.forms import SecretFormPrefill, SecretFormSession
from octoforge_core.secrets.policy import (
    host_matches,
    normalize_code,
    normalize_description,
    normalize_host,
    normalize_placements,
    normalize_transform,
)
from octoforge_core.secrets.ports import SecretFormLinkFactory, SecretFormLinkStore, SecretStore
from octoforge_core.secrets.transforms import apply_transform
from octoforge_core.secrets.types import (
    DEFAULT_PLACEMENTS,
    InvalidSecretError,
    ResolvedSecret,
    SecretHostMismatchError,
    SecretInfo,
    SecretNotFoundError,
    SecretPlacement,
    SecretTransform,
    SecretWrite,
)

__all__ = [
    "DEFAULT_PLACEMENTS",
    "InvalidSecretError",
    "ResolvedSecret",
    "SecretFormLinkFactory",
    "SecretFormLinkStore",
    "SecretFormPrefill",
    "SecretFormSession",
    "SecretHostMismatchError",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretPlacement",
    "SecretStore",
    "SecretTransform",
    "SecretWrite",
    "apply_transform",
    "host_matches",
    "normalize_code",
    "normalize_description",
    "normalize_host",
    "normalize_placements",
    "normalize_transform",
]
