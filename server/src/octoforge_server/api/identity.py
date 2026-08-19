"""Identity endpoints: how an out-of-process surface mirrors its profiles.

The ingestion node knows what Telegram currently calls a sender — and nothing
else about them. Names, like plans and statuses, key on people, and only this
service can turn an account into a person. So the node reports the profile
here, on every contact, exactly as the in-process arrangement writes it into
the identity store directly.

Deliberately no status gate: a person still WAITING is precisely the one whose
name the operator needs — the console's queue is where activation is decided,
and a queue of bare ids gives the operator nothing to decide by. Recording a
name burns no model call, so there is nothing here for a banned account to
abuse either.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from octoforge_core.identity.api import IdentityStore
from pydantic import BaseModel

from octoforge_server.deps import get_channel, get_external_id, get_identity_store

router = APIRouter(prefix="/api/identity")

ExternalIdDep = Annotated[str, Depends(get_external_id)]
ChannelDep = Annotated[str, Depends(get_channel)]
IdentityStoreDep = Annotated[IdentityStore, Depends(get_identity_store)]


class ProfileUpdateRequest(BaseModel):
    """What the surface currently calls this account."""

    name: str = ""
    username: str | None = None


@router.put("/profile", status_code=HTTPStatus.NO_CONTENT)
async def put_profile(
    request: ProfileUpdateRequest,
    external_id: ExternalIdDep,
    channel: ChannelDep,
    identities: IdentityStoreDep,
) -> None:
    """Mirror the surface profile of one account, minting the person if needed.

    Minting is safe and necessary: whether the account may talk at all was
    decided by the gate in front of the relay (the invite gate on the node,
    the service credential here), and on first contact the profile arrives
    before the first message has — a mirror that only updated would leave
    every newcomer nameless until their second message.
    """
    await identities.resolve_or_create(channel, external_id)
    await identities.update_profile(channel, external_id, request.name, request.username)
