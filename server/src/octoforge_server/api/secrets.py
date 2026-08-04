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
from octoforge_server.secret_links import RedeemedLink, SecretLinkService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/secrets")

SECRETS_DISABLED_DETAIL = "secrets are not configured on this installation"
EXPIRED_TOKEN_DETAIL = "the link has expired; ask the assistant or run /secrets for a fresh one"
UNKNOWN_TOKEN_DETAIL = (
    "this link is not valid — it was never issued by this installation, or it has "
    "already been cleaned up. Ask the assistant for a new one (or run /secrets)"
)

StoreDep = Annotated[SecretStore | None, Depends(get_secret_store)]
LinksDep = Annotated[SecretLinkService, Depends(get_secret_links)]
IdentityDep = Annotated[IdentityStore, Depends(get_identity_store)]


class SetSecretRequest(BaseModel):
    """Payload of the add/replace form; the value never appears in responses."""

    token: str
    code: str
    value: str = Field(repr=False)
    allowed_host: str
    description: str
    placements: list[str] = []
    transform: str | None = None


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
) -> RedeemedLink:
    """The redeemed link (person resolved), or an HTTP error.

    An account token names an *account* on a surface; the store is keyed by
    person. Turning one into the other is the service's job — the same
    resolution `X-User-Id` gets — and doing it anywhere else is what broke
    this form: the link named the account, the form wrote under it, and the
    agent read under the person, so every secret saved after the identity
    migration was invisible without a single error. A person token (minted
    by the agent's secret_link tool, which runs inside the service) names
    the person directly and may carry form prefill.

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
    redeemed = await links.redeem_any(token)
    if redeemed is None:
        # "expired" and "never existed" are different situations for whoever
        # holds the link: one is fixed by asking again, the other means the
        # link they were given was not real
        detail = EXPIRED_TOKEN_DETAIL if await links.expired(token) else UNKNOWN_TOKEN_DETAIL
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=detail)
    if redeemed.user_id is not None:
        return redeemed
    assert redeemed.subject is not None  # redeem sets exactly one of the two
    user_id = await identities.resolve_or_create(
        redeemed.subject.surface, redeemed.subject.external_id
    )
    return RedeemedLink(user_id=user_id, prefill=redeemed.prefill)


@router.post("/session")
async def session(
    request: SessionRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, Any]:
    """Validate the token; list the user's secrets metadata and any prefill.

    Values never appear; the prefill is what the agent's secret_link tool
    put into the token — everything of one secret except the value.
    """
    redeemed = await _authorize(links, store, identities, request.token)
    assert store is not None  # _authorize raised otherwise
    assert redeemed.user_id is not None
    infos = await store.list(redeemed.user_id)
    prefill = redeemed.prefill
    return {
        "secrets": [
            {
                "code": info.code,
                "allowed_host": info.allowed_host,
                "description": info.description,
                "placements": sorted(member.value for member in info.placements),
                "transform": info.transform.value if info.transform is not None else None,
                "created_at": info.created_at.isoformat(),
                "last_used_at": (
                    info.last_used_at.isoformat() if info.last_used_at is not None else None
                ),
            }
            for info in infos
        ],
        "prefill": (
            {
                "code": prefill.code,
                "allowed_host": prefill.allowed_host,
                "description": prefill.description,
                "placements": sorted(member.value for member in prefill.placements),
                "transform": prefill.transform.value if prefill.transform is not None else None,
            }
            if prefill is not None
            else None
        ),
    }


@router.post("/set")
async def set_secret(
    request: SetSecretRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, str]:
    """Store or replace one secret for the token's user."""
    redeemed = await _authorize(links, store, identities, request.token)
    assert store is not None
    assert redeemed.user_id is not None
    try:
        info = await store.put(
            redeemed.user_id,
            request.code,
            request.value,
            request.allowed_host,
            request.description,
            placements=request.placements,
            transform=request.transform,
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
    request: DeleteSecretRequest, store: StoreDep, links: LinksDep, identities: IdentityDep
) -> dict[str, str]:
    """Delete one secret for the token's user."""
    redeemed = await _authorize(links, store, identities, request.token)
    assert store is not None
    assert redeemed.user_id is not None
    try:
        await store.delete(redeemed.user_id, request.code)
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidSecretError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "deleted"}
