"""Storage and link ports of the secrets module."""

from typing import Protocol

from octoforge_core.secrets.forms import SecretFormPrefill, SecretFormSession
from octoforge_core.secrets.types import ResolvedSecret, SecretInfo, SecretWrite


class SecretFormLinkFactory(Protocol):
    """Mint one-time secret form URLs bound to a person."""

    async def build_prefilled(self, user_id: str, prefill: SecretFormPrefill) -> str:
        """Return a short-lived form URL with everything but the value filled."""
        ...


class SecretFormLinkStore(Protocol):
    """Persist short capability codes for the secrets form."""

    async def issue(
        self,
        user_id: str,
        prefill: SecretFormPrefill | None,
        ttl_seconds: float,
    ) -> str:
        """Store a fresh code and return it."""
        ...

    async def redeem(self, code: str) -> SecretFormSession | None:
        """Return what a live code opens, or None."""
        ...

    async def is_expired(self, code: str) -> bool:
        """Whether this code existed and has run out."""
        ...


class SecretStore(Protocol):
    """Encrypted per-user secrets, readable only through host-bound resolve."""

    async def put(self, request: SecretWrite) -> SecretInfo:
        """Store or replace one secret."""
        ...

    async def list(self, user_id: str) -> list[SecretInfo]:
        """Return metadata only, newest first."""
        ...

    async def delete(self, user_id: str, code: str) -> None:
        """Delete one secret or raise when absent."""
        ...

    async def resolve(self, user_id: str, code: str, host: str) -> ResolvedSecret:
        """Resolve one secret for a request to host and stamp its usage time."""
        ...
