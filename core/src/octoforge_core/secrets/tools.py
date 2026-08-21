"""Agent-facing secrets tools: metadata listing and pre-filled form links.

Neither tool ever touches a value. `secret_list` is how the model tells two
secrets for one host apart (that is what descriptions are for) and how it
notices one is missing or undocumented; `secret_link` is how a missing
secret gets fixed — the agent mints a one-time form URL with every field but
the value filled in, and the user only pastes the secret itself.
"""

from typing import Any

from octoforge_core.secrets.api import (
    InvalidSecretError,
    SecretFormLinkFactory,
    SecretFormPrefill,
    SecretInfo,
    SecretStore,
    normalize_code,
    normalize_description,
    normalize_host,
    normalize_placements,
    normalize_transform,
)
from octoforge_core.secrets.tool_contract import (
    LINK_DESCRIPTION,
    LINK_MESSAGE_TEMPLATE,
    LINK_NAME,
    LINK_SCHEMA,
    LIST_DESCRIPTION,
    LIST_NAME,
    LIST_SCHEMA,
    NO_SECRETS_MESSAGE,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class SecretListTool:
    """Metadata-only listing of the caller's secrets."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LIST_NAME,
            description=LIST_DESCRIPTION,
            parameters_schema=LIST_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """List the caller's secrets; values are absent by construction."""
        infos = await self._store.list(context.user_id)
        if not infos:
            return NO_SECRETS_MESSAGE
        return "\n".join(_format_info(info) for info in infos)


class SecretLinkTool:
    """Mints pre-filled one-time secrets-form links."""

    def __init__(self, links: SecretFormLinkFactory) -> None:
        self._links = links

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LINK_NAME,
            description=LINK_DESCRIPTION,
            parameters_schema=LINK_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate the prefill and return the minted URL with instructions."""
        prefill = _parse_prefill(arguments)
        url = await self._links.build_prefilled(context.user_id, prefill)
        return LINK_MESSAGE_TEMPLATE.format(url=url)


def _parse_prefill(arguments: dict[str, Any]) -> SecretFormPrefill:
    raw_placements = arguments.get("placements") or []
    if not isinstance(raw_placements, list) or not all(
        isinstance(item, str) for item in raw_placements
    ):
        raise ToolArgumentsError("placements must be an array of strings")
    raw_transform = arguments.get("transform")
    if raw_transform is not None and not isinstance(raw_transform, str):
        raise ToolArgumentsError("transform must be a string")
    try:
        return SecretFormPrefill(
            code=normalize_code(_required_str(arguments, "code")),
            allowed_host=normalize_host(_required_str(arguments, "host")),
            description=normalize_description(_required_str(arguments, "description")),
            placements=normalize_placements(raw_placements),
            transform=normalize_transform(raw_transform),
        )
    except InvalidSecretError as exc:
        raise ToolArgumentsError(str(exc)) from exc


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentsError(f"{key} must be a non-empty string")
    return value


def _format_info(info: SecretInfo) -> str:
    placements = ",".join(sorted(member.value for member in info.placements))
    transform = info.transform.value if info.transform is not None else "none"
    last_used = info.last_used_at.isoformat() if info.last_used_at is not None else "never"
    return (
        f"- {info.code} → {info.allowed_host}; placements: {placements}; "
        f"transform: {transform}; last used: {last_used}\n  {info.description}"
    )
