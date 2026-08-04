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
    SecretTransform,
    normalize_code,
    normalize_description,
    normalize_host,
    normalize_placements,
    normalize_transform,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

LIST_NAME = "secret_list"
LIST_DESCRIPTION = (
    "List the user's stored secrets: code, allowed host, description, allowed "
    "placements and transform — never the values. Use it to pick the right "
    "secret code for an endpoint (descriptions say what each one is for) and "
    "to see what is missing; a description can be updated with a secret_link."
)
LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
NO_SECRETS_MESSAGE = (
    "no secrets stored for this user; mint a pre-filled form link with "
    "secret_link when an endpoint needs one"
)

LINK_NAME = "secret_link"
LINK_DESCRIPTION = (
    "Mint a one-time link to the secrets form with every field pre-filled "
    "except the value; send the link to the user, who only pastes the secret "
    "itself. Use it when an endpoint reports a missing secret (take code and "
    "host from its message) or to update a secret's metadata. Fill "
    "`description` with what the secret is for in the user's own words — it "
    "is how the right secret is picked later. The link expires in about 10 "
    "minutes and never carries the value."
)
LINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Secret code, 1-64 chars of [a-z0-9_], e.g. 'gmail_token'",
        },
        "host": {
            "type": "string",
            "description": (
                "The only host the secret may be sent to, e.g. 'api.example.com' — "
                "take it from the endpoint's URL or its missing-secret message. "
                "For a service that shards across sibling hosts (iCloud answers on "
                "p54-caldav.icloud.com after discovery) use a one-level pattern "
                "like '*.icloud.com'; keep it as narrow as the service allows"
            ),
        },
        "description": {
            "type": "string",
            "description": "What the secret is for, e.g. 'read-only token for the work calendar'",
        },
        "placements": {
            "type": "array",
            "items": {"type": "string", "enum": ["header", "url", "body"]},
            "description": (
                "Request parts the secret may be substituted into; omit for the "
                "default (header only)"
            ),
        },
        "transform": {
            "type": "string",
            "enum": [member.value for member in SecretTransform],
            "description": (
                "Static transform applied before substitution; e.g. 'base64' "
                "for HTTP Basic, where the stored value is 'user:password'. "
                "Omit to send the value as stored"
            ),
        },
    },
    "required": ["code", "host", "description"],
}
LINK_MESSAGE_TEMPLATE = (
    "One-time secrets-form link (expires in ~10 minutes), everything but the "
    "value is pre-filled:\n{url}\nSend it to the user; they only paste the "
    "secret value. The link never contains the value."
)


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
        url = self._links.build_prefilled(context.user_id, prefill)
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
