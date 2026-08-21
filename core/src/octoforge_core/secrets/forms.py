"""Values carried by one-time secret form links."""

from dataclasses import dataclass

from octoforge_core.secrets.types import DEFAULT_PLACEMENTS, SecretPlacement, SecretTransform


@dataclass(frozen=True, slots=True)
class SecretFormPrefill:
    """Everything of one secret except the value, for a pre-filled form link."""

    code: str
    allowed_host: str
    description: str
    placements: frozenset[SecretPlacement] = DEFAULT_PLACEMENTS
    transform: SecretTransform | None = None


@dataclass(frozen=True, slots=True)
class SecretFormSession:
    """What a redeemed form code opens: whose secrets, and any prefill."""

    user_id: str
    prefill: SecretFormPrefill | None = None
