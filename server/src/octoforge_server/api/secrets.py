"""Self-service secrets form API: token-scoped, outside the operator gate.

These endpoints are the only HTTP surface a dialog user (not the operator)
talks to, so they sit outside the Basic-auth middleware: the one-time token
from /secrets in Telegram IS the authentication, and every handler resolves
it into a user id before touching anything. Values are accepted, never
returned — the form is write-and-forget by design.
"""

import logging
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from octoforge_core.identity.api import IdentityStore
from octoforge_core.secrets.api import (
    InvalidSecretError,
    SecretNotFoundError,
    SecretStore,
)
from pydantic import BaseModel, Field

from octoforge_server.deps import get_identity_store, get_secret_links, get_secret_store
from octoforge_server.secret_links import SecretLinkService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/secrets")

SECRETS_DISABLED_DETAIL = "secrets are not configured on this installation"
BAD_TOKEN_DETAIL = "the link has expired; run /secrets in Telegram to get a fresh one"

StoreDep = Annotated[SecretStore | None, Depends(get_secret_store)]
LinksDep = Annotated[SecretLinkService, Depends(get_secret_links)]
IdentityDep = Annotated[IdentityStore, Depends(get_identity_store)]


class SetSecretRequest(BaseModel):
    """Payload of the add/replace form; the value never appears in responses."""

    token: str
    code: str
    value: str = Field(repr=False)
    allowed_host: str


class DeleteSecretRequest(BaseModel):
    token: str
    code: str


class SessionRequest(BaseModel):
    """Token of the form session; a body rather than a query parameter.

    A query string is logged by every proxy and kept in browser history, and
    this token is a capability over one user's secrets.
    """

    token: str


async def _authorize(
    links: SecretLinkService,
    store: SecretStore | None,
    identities: IdentityStore,
    token: str,
) -> str:
    """The person whose secrets this token opens, or an HTTP error.

    The token names an *account* on a surface; the store is keyed by person.
    Turning one into the other is the service's job — the same resolution
    `X-User-Id` gets — and doing it anywhere else is what broke this form:
    the link named the account, the form wrote under it, and the agent read
    under the person, so every secret saved after the identity migration was
    invisible without a single error.

    `resolve_or_create`, deliberately. `/secrets` is intercepted before the
    dialog pipeline, so somebody whose very first message is that command has
    passed the invite gate but has no person yet — `resolve` alone would
    refuse them a form they are entitled to, permanently. Minting here is the
    same thing their first ordinary message would have done, and the token is
    unforgeable and only ever issued by a surface that already let them in.
    """
    if store is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=SECRETS_DISABLED_DETAIL
        )
    subject = links.redeem(token)
    if subject is None:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=BAD_TOKEN_DETAIL)
    return await identities.resolve_or_create(subject.surface, subject.external_id)


@router.post("/session")
async def session(
    request: SessionRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, Any]:
    """Validate the token and list the user's secrets metadata (never values)."""
    user_id = await _authorize(links, store, identities, request.token)
    assert store is not None  # _authorize raised otherwise
    infos = await store.list(user_id)
    return {
        "secrets": [
            {
                "code": info.code,
                "allowed_host": info.allowed_host,
                "created_at": info.created_at.isoformat(),
                "last_used_at": (
                    info.last_used_at.isoformat() if info.last_used_at is not None else None
                ),
            }
            for info in infos
        ]
    }


@router.post("/set")
async def set_secret(
    request: SetSecretRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, str]:
    """Store or replace one secret for the token's user."""
    user_id = await _authorize(links, store, identities, request.token)
    assert store is not None
    try:
        info = await store.put(user_id, request.code, request.value, request.allowed_host)
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    logger.info("secret stored: user=%s code=%s host=%s", user_id, info.code, info.allowed_host)
    return {"status": "stored", "code": info.code}


@router.post("/delete")
async def delete_secret(
    request: DeleteSecretRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, str]:
    """Delete one secret for the token's user."""
    user_id = await _authorize(links, store, identities, request.token)
    assert store is not None
    try:
        await store.delete(user_id, request.code)
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "deleted"}
