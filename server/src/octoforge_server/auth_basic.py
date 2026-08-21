"""HTTP Basic header decoding and standard authentication failures."""

import base64
from http import HTTPStatus

from fastapi import HTTPException, Request

WWW_AUTHENTICATE = {"WWW-Authenticate": 'Basic realm="OctoForge"'}
BASIC_PREFIX = "basic "
UNAUTHORIZED_MESSAGE = "authentication required"
UNKNOWN_CLIENT = "unknown"


def decode_basic(header: str) -> tuple[str, str] | None:
    try:
        decoded = base64.b64decode(header[len(BASIC_PREFIX) :].strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    user, _, password = decoded.partition(":")
    return user, password


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=UNAUTHORIZED_MESSAGE,
        headers=dict(WWW_AUTHENTICATE),
    )


def client_address(request: Request) -> str:
    return UNKNOWN_CLIENT if request.client is None else request.client.host
