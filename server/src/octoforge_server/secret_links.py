"""One-time account/person capabilities for the self-service secrets form."""

from octoforge_core.secrets.api import SecretFormLinkStore, SecretFormPrefill

from octoforge_server.link_tokens import (
    LinkSubject,
    LinkTokenCodec,
    RedeemedLink,
    derive_link_key,
)
from octoforge_server.secrets_link_builders import (
    PersonSecretsLinkBuilder,
    secrets_link_builder,
)

TOKEN_TTL_SECONDS = 600.0

__all__ = [
    "LinkSubject",
    "PersonSecretsLinkBuilder",
    "RedeemedLink",
    "SecretLinkService",
    "derive_link_key",
    "secrets_link_builder",
]


class SecretLinkService:
    """Combine stored short codes with stateless encrypted account tokens."""

    def __init__(
        self,
        key: str = "",
        ttl_seconds: float = TOKEN_TTL_SECONDS,
        codes: SecretFormLinkStore | None = None,
    ) -> None:
        self._tokens = LinkTokenCodec(key)
        self.ttl_seconds = ttl_seconds
        self._codes = codes

    async def issue_code(self, user_id: str, prefill: SecretFormPrefill | None = None) -> str:
        if self._codes is None:
            raise RuntimeError("short form codes need a link store")
        return await self._codes.issue(user_id, prefill, self.ttl_seconds)

    def issue(self, subject: LinkSubject) -> str:
        return self._tokens.issue(subject)

    def issue_for_person(self, user_id: str, prefill: SecretFormPrefill) -> str:
        return self._tokens.issue_for_person(user_id, prefill)

    async def redeem_any(self, token: str) -> RedeemedLink | None:
        if self._codes is not None:
            session = await self._codes.redeem(token)
            if session is not None:
                return RedeemedLink(user_id=session.user_id, prefill=session.prefill)
        return self.redeem(token)

    async def expired(self, token: str) -> bool:
        if self._codes is not None and await self._codes.is_expired(token):
            return True
        return self._tokens.existed(token)

    def redeem(self, token: str) -> RedeemedLink | None:
        return self._tokens.redeem(token, self.ttl_seconds)
