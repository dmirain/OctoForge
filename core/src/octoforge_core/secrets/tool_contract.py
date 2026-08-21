"""Model-facing names, descriptions and schemas for secret tools."""

from typing import Any

from octoforge_core.secrets.types import SecretTransform

LIST_NAME = "secret_list"
LIST_DESCRIPTION = (
    "List the user's stored secrets: code, allowed host, description, allowed "
    "placements and transform - never the values. Use it to pick the right "
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
    "`description` with what the secret is for in the user's own words - it "
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
                "The only host the secret may be sent to, e.g. 'api.example.com' - "
                "take it from the endpoint's URL or its missing-secret message. "
                "For a service that shards across sibling hosts use a one-level "
                "pattern like '*.icloud.com'; keep it as narrow as possible"
            ),
        },
        "description": {
            "type": "string",
            "description": "What the secret is for, e.g. 'read-only mailbox token'",
        },
        "placements": {
            "type": "array",
            "items": {"type": "string", "enum": ["header", "url", "body"]},
            "description": "Request parts allowed for substitution; default is header only",
        },
        "transform": {
            "type": "string",
            "enum": [member.value for member in SecretTransform],
            "description": "Static transform applied before substitution; omit for none",
        },
    },
    "required": ["code", "host", "description"],
}
LINK_MESSAGE_TEMPLATE = (
    "One-time secrets-form link (expires in ~10 minutes), everything but the "
    "value is pre-filled:\n{url}\nSend it to the user; they only paste the "
    "secret value. The link never contains the value."
)
