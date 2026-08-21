"""Token-scoped self-service secret form endpoints."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.secrets.api import InvalidSecretError, SecretNotFoundError, SecretWrite

from octoforge_server.api.secret_deps import SecretServices, SecretServicesDep
from octoforge_server.api.secret_response import session_response
from octoforge_server.api.secret_schemas import (
    DeleteSecretRequest,
    SessionRequest,
    SetSecretRequest,
)
from octoforge_server.secret_links import RedeemedLink

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/secrets")

SECRETS_DISABLED_DETAIL = "secrets are not configured on this installation"
EXPIRED_TOKEN_DETAIL = "the link has expired; ask the assistant or run /secrets for a fresh one"
UNKNOWN_TOKEN_DETAIL = (
    "this link is not valid - it was never issued by this installation, or it has "
    "already been cleaned up. Ask the assistant for a new one (or run /secrets)"
)


async def _authorize(services: SecretServices, token: str) -> RedeemedLink:
    """Resolve one capability token to the person whose secrets it controls."""
    if services.store is None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=SECRETS_DISABLED_DETAIL,
        )
    redeemed = await services.links.redeem_any(token)
    if redeemed is None:
        detail = (
            EXPIRED_TOKEN_DETAIL if await services.links.expired(token) else UNKNOWN_TOKEN_DETAIL
        )
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=detail)
    if redeemed.user_id is not None:
        return redeemed
    assert redeemed.subject is not None
    user_id = await services.identities.resolve_or_create(
        redeemed.subject.surface,
        redeemed.subject.external_id,
    )
    return RedeemedLink(user_id=user_id, prefill=redeemed.prefill)


@router.post("/session")
async def session(request: SessionRequest, services: SecretServicesDep) -> dict[str, Any]:
    """Validate a token and return secret metadata plus any form prefill."""
    redeemed = await _authorize(services, request.token)
    assert services.store is not None
    assert redeemed.user_id is not None
    infos = await services.store.list(redeemed.user_id)
    return session_response(infos, redeemed.prefill)


@router.post("/set")
async def set_secret(request: SetSecretRequest, services: SecretServicesDep) -> dict[str, str]:
    """Store or replace one secret for the token's user."""
    redeemed = await _authorize(services, request.token)
    assert services.store is not None
    assert redeemed.user_id is not None
    try:
        info = await services.store.put(
            SecretWrite(
                redeemed.user_id,
                request.code,
                request.value,
                request.allowed_host,
                request.description,
                tuple(request.placements),
                request.transform,
            )
        )
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "secret stored: user=%s code=%s host=%s",
        redeemed.user_id,
        info.code,
        info.allowed_host,
    )
    return {"status": "stored", "code": info.code}


@router.post("/delete")
async def delete_secret(
    request: DeleteSecretRequest,
    services: SecretServicesDep,
) -> dict[str, str]:
    """Delete one secret for the token's user."""
    redeemed = await _authorize(services, request.token)
    assert services.store is not None
    assert redeemed.user_id is not None
    try:
        await services.store.delete(redeemed.user_id, request.code)
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "deleted"}
