"""Channel validation and account-to-person admission dependencies."""

from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request
from octoforge_core.identity.api import UserStatus

from octoforge_server.state_deps import get_access_service, get_identity_store

CHANNEL_HEADER = "X-Channel"
UNKNOWN_CHANNEL_MESSAGE = "unknown channel: {channel}"
MISSING_USER_ID_MESSAGE = "X-User-Id header is required"
ACCESS_WAITING_MESSAGE = "registration is pending: no free slots right now"
ACCESS_BANNED_MESSAGE = "access is closed"
ACCESS_STATUS_HEADER = "X-Access-Status"


def get_channel(request: Request) -> str:
    declared = cast(str, request.app.state.channel)
    channel = request.headers.get(CHANNEL_HEADER)
    if channel is None:
        return declared
    known = cast(frozenset[str], request.app.state.channels)
    if channel not in known:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=UNKNOWN_CHANNEL_MESSAGE.format(channel=channel),
        )
    return channel


def get_external_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    if x_user_id is None or not x_user_id.strip():
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=MISSING_USER_ID_MESSAGE)
    return x_user_id.strip()


async def get_user_id(
    request: Request,
    external_id: Annotated[str, Depends(get_external_id)],
    channel: Annotated[str, Depends(get_channel)],
) -> str:
    person = await get_identity_store(request).resolve_or_create(channel, external_id)
    status = await get_access_service(request).admit(person)
    if status is UserStatus.BANNED:
        raise _access_refused(ACCESS_BANNED_MESSAGE, status)
    if status is not UserStatus.ACTIVE:
        raise _access_refused(ACCESS_WAITING_MESSAGE, status)
    return person


def _access_refused(message: str, status: UserStatus) -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.FORBIDDEN,
        detail=message,
        headers={ACCESS_STATUS_HEADER: status.value},
    )
