"""Tests for the agent-facing secrets tools: listing metadata, minting links."""

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from octoforge_core.secrets.api import (
    DEFAULT_PLACEMENTS,
    ResolvedSecret,
    SecretFormPrefill,
    SecretInfo,
    SecretPlacement,
    SecretTransform,
)
from octoforge_core.secrets.tools import SecretLinkTool, SecretListTool
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = ToolContext(user_id="person-1", channel="web", dialog_id="d-1")


class ScriptedSecretStore:
    """SecretStore stub answering list() with scripted metadata."""

    def __init__(self, infos: list[SecretInfo]) -> None:
        self._infos = infos

    async def put(  # noqa: PLR0913, PLR0917 — mirrors the port
        self,
        user_id: str,
        code: str,
        value: str,
        allowed_host: str,
        description: str,
        placements: Iterable[str] = (),
        transform: str | None = None,
    ) -> SecretInfo:
        raise NotImplementedError

    async def list(self, user_id: str) -> list[SecretInfo]:
        return list(self._infos)

    async def delete(self, user_id: str, code: str) -> None:
        raise NotImplementedError

    async def resolve(self, user_id: str, code: str, host: str) -> ResolvedSecret:
        raise NotImplementedError


class RecordingLinkFactory:
    """SecretFormLinkFactory stub recording what it was asked to mint."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, SecretFormPrefill]] = []

    async def build_prefilled(self, user_id: str, prefill: SecretFormPrefill) -> str:
        self.calls.append((user_id, prefill))
        return "https://forge.example.com/secrets.html#t=Ab3xK9pQ"


def info(code: str, description: str) -> SecretInfo:
    return SecretInfo(
        code=code,
        allowed_host="api.example.com",
        description=description,
        placements=DEFAULT_PLACEMENTS,
        transform=None,
        created_at=CREATED_AT,
        last_used_at=None,
    )


async def test_secret_list_shows_metadata_with_descriptions() -> None:
    """Descriptions are what let the model tell two secrets for one host apart."""
    tool = SecretListTool(
        ScriptedSecretStore(
            [info("mail_token", "work mailbox"), info("cal_token", "the shared calendar")]
        )
    )

    output = await tool.execute({}, CONTEXT)

    assert "mail_token" in output
    assert "work mailbox" in output
    assert "the shared calendar" in output


async def test_secret_list_explains_an_empty_store() -> None:
    tool = SecretListTool(ScriptedSecretStore([]))

    assert "no secrets" in await tool.execute({}, CONTEXT)


async def test_secret_link_mints_a_prefilled_url_for_the_calling_user() -> None:
    factory = RecordingLinkFactory()
    tool = SecretLinkTool(factory)

    output = await tool.execute(
        {
            "code": "Mail_Token",
            "host": "API.example.com.",
            "description": "  token for the   mailbox ",
            "placements": ["header", "url"],
            "transform": "base64",
        },
        CONTEXT,
    )

    ((user_id, prefill),) = factory.calls
    assert user_id == "person-1"
    assert prefill.code == "mail_token"  # normalized like the store would
    assert prefill.allowed_host == "api.example.com"
    assert prefill.description == "token for the mailbox"
    assert prefill.placements == frozenset({SecretPlacement.HEADER, SecretPlacement.URL})
    assert prefill.transform is SecretTransform.BASE64
    assert "https://forge.example.com/secrets.html#t=Ab3xK9pQ" in output


@pytest.mark.parametrize(
    "arguments",
    [
        {"host": "api.example.com", "description": "x"},  # no code
        {"code": "tok", "description": "x"},  # no host
        {"code": "tok", "host": "api.example.com"},  # no description
        {"code": "tok", "host": "api.example.com", "description": "   "},
        {"code": "tok", "host": "https://api.example.com/x", "description": "x"},
        {"code": "tok", "host": "api.example.com", "description": "x", "placements": ["cookie"]},
        {"code": "tok", "host": "api.example.com", "description": "x", "transform": "rot13"},
    ],
)
async def test_secret_link_rejects_malformed_arguments(arguments: dict[str, object]) -> None:
    tool = SecretLinkTool(RecordingLinkFactory())

    with pytest.raises(ToolArgumentsError):
        await tool.execute(dict(arguments), CONTEXT)
