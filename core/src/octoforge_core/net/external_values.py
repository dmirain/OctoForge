"""Resolve per-user values and secrets under host and placement policy."""

from collections.abc import Iterable, Mapping
from urllib.parse import quote, urlsplit

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external_messages import (
    PARAM_MISSING_TEMPLATE,
    PARAMS_DISABLED_MESSAGE,
    PLACEMENT_BLOCKED_TEMPLATE,
    SECRET_MISSING_TEMPLATE,
    SECRET_SCRUBBED,
    SECRETS_DISABLED_MESSAGE,
)
from octoforge_core.net.external_types import CallCredentials
from octoforge_core.net.spec_types import TemplateRefs
from octoforge_core.secrets.api import (
    InvalidSecretError,
    ResolvedSecret,
    SecretHostMismatchError,
    SecretNotFoundError,
    SecretPlacement,
)


class ExternalValues:
    """Load server-side template values without exposing them to model context."""

    def __init__(self, credentials: CallCredentials) -> None:
        self._secrets = credentials.secrets
        self._user_params = credentials.user_params

    async def user_values(self, refs: TemplateRefs, user_id: str | None) -> dict[str, str]:
        codes = refs.user_params
        if not codes:
            return {}
        if self._user_params is None:
            raise ExternalCallError(PARAMS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError("this endpoint uses per-user params: no user in context")
        stored = await self._user_params.get_for_user(user_id)
        missing = sorted(codes - stored.keys())
        if missing:
            raise ExternalCallError(
                PARAM_MISSING_TEMPLATE.format(codes=", ".join(f"'{code}'" for code in missing))
            )
        return {f"user.{code}": stored[code] for code in codes}

    async def secrets(
        self,
        refs: TemplateRefs,
        url: str,
        user_id: str | None,
    ) -> dict[str, ResolvedSecret]:
        if not refs.secrets:
            return {}
        if self._secrets is None:
            raise ExternalCallError(SECRETS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError("this endpoint requires a per-user secret: no user in context")
        host = (urlsplit(url).hostname or "").lower()
        resolved: dict[str, ResolvedSecret] = {}
        for code in sorted(refs.secrets):
            try:
                resolved[code] = await self._secrets.resolve(user_id, code, host)
            except SecretNotFoundError:
                raise ExternalCallError(
                    SECRET_MISSING_TEMPLATE.format(code=code, host=host)
                ) from None
            except (SecretHostMismatchError, InvalidSecretError) as exc:
                raise ExternalCallError(str(exc)) from None
        return resolved


def enforce_placements(
    parts: tuple[tuple[SecretPlacement, frozenset[str]], ...],
    resolved: Mapping[str, ResolvedSecret],
) -> None:
    for placement, codes in parts:
        for code in sorted(codes):
            allowed = resolved[code].placements
            if placement not in allowed:
                raise ExternalCallError(
                    PLACEMENT_BLOCKED_TEMPLATE.format(
                        code=code,
                        part=placement.value,
                        allowed=", ".join(sorted(member.value for member in allowed)),
                    )
                )


def scrub(body: str, secrets: Iterable[ResolvedSecret]) -> str:
    for secret in secrets:
        values = {
            secret.value,
            secret.plain,
            quote(secret.value, safe=""),
            quote(secret.plain, safe=""),
        }
        for value in values:
            body = body.replace(value, SECRET_SCRUBBED)
    return body
