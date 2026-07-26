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
from octoforge_core.secrets.api import (
    InvalidSecretError,
    SecretNotFoundError,
    SecretStore,
)
from pydantic import BaseModel, Field

from octoforge_web.deps import get_secret_links, get_secret_store
from octoforge_web.secret_links import SecretLinkService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/secrets")

SECRETS_DISABLED_DETAIL = "secrets are not configured on this installation"
BAD_TOKEN_DETAIL = "the link has expired; run /secrets in Telegram to get a fresh one"

StoreDep = Annotated[SecretStore | None, Depends(get_secret_store)]
LinksDep = Annotated[SecretLinkService, Depends(get_secret_links)]


class SetSecretRequest(BaseModel):
    """Payload of the add/replace form; the value never appears in responses."""

    token: str
    code: str
    value: str = Field(repr=False)
    allowed_host: str


class DeleteSecretRequest(BaseModel):
    token: str
    code: str


def _authorize(links: SecretLinkService, store: SecretStore | None, token: str) -> str:
    if store is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail=SECRETS_DISABLED_DETAIL
        )
    user_id = links.redeem(token)
    if user_id is None:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=BAD_TOKEN_DETAIL)
    return user_id


@router.get("/session")
async def session(token: str, store: StoreDep, links: LinksDep) -> dict[str, Any]:
    """Validate the token and list the user's secrets metadata (never values)."""
    user_id = _authorize(links, store, token)
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
async def set_secret(request: SetSecretRequest, store: StoreDep, links: LinksDep) -> dict[str, str]:
    """Store or replace one secret for the token's user."""
    user_id = _authorize(links, store, request.token)
    assert store is not None
    try:
        info = await store.put(user_id, request.code, request.value, request.allowed_host)
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    logger.info("secret stored: user=%s code=%s host=%s", user_id, info.code, info.allowed_host)
    return {"status": "stored", "code": info.code}


@router.post("/delete")
async def delete_secret(
    request: DeleteSecretRequest, store: StoreDep, links: LinksDep
) -> dict[str, str]:
    """Delete one secret for the token's user."""
    user_id = _authorize(links, store, request.token)
    assert store is not None
    try:
        await store.delete(user_id, request.code)
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "deleted"}
