"""URL builders for account and person secret-form capabilities."""

from collections.abc import Callable
from typing import Protocol

from octoforge_core.secrets.api import SecretFormPrefill

from octoforge_server.config import Settings
from octoforge_server.link_tokens import LinkSubject


class LinkIssuer(Protocol):
    def issue(self, subject: LinkSubject) -> str: ...

    async def issue_code(self, user_id: str, prefill: SecretFormPrefill | None) -> str: ...


def secrets_link_builder(
    settings: Settings,
    links: LinkIssuer,
    surface: str,
) -> Callable[[str], str]:
    base = settings.resolved_public_base_url()

    def build(external_id: str) -> str:
        token = links.issue(LinkSubject(surface, external_id))
        return f"{base}/secrets.html#token={token}"

    return build


class PersonSecretsLinkBuilder:
    def __init__(self, settings: Settings, links: LinkIssuer) -> None:
        self._base = settings.resolved_public_base_url()
        self._links = links

    async def build_prefilled(self, user_id: str, prefill: SecretFormPrefill) -> str:
        code = await self._links.issue_code(user_id, prefill)
        return f"{self._base}/secrets.html#t={code}"
