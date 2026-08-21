"""HTTP credential and cross-site request guard."""

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from octoforge_server.auth import (
    CROSS_SITE_MESSAGE,
    AuthGate,
    allows_service_credential,
    is_cross_site_mutation,
    is_open_path,
)
from octoforge_server.config import Settings

logger = logging.getLogger(__name__)
NextCall = Callable[[Request], Awaitable[Response]]


def install_guard(app: FastAPI, settings: Settings) -> None:
    """Guard every route except the health probes, including static files and docs."""
    gate = AuthGate(
        username=settings.admin_username,
        password_hash=settings.admin_password_hash,
        service_username=settings.service_username,
        service_password_hash=settings.service_password_hash,
    )
    app.state.auth_gate = gate

    @app.middleware("http")
    async def authenticate(request: Request, call_next: NextCall) -> Response:
        if is_cross_site_mutation(request):
            logger.warning("cross-site %s to %s refused", request.method, request.url.path)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": CROSS_SITE_MESSAGE},
            )
        if is_open_path(request.url.path):
            return await call_next(request)
        try:
            await gate.authenticate(
                request, service_allowed=allows_service_credential(request.url.path)
            )
        except HTTPException as denied:
            return JSONResponse(
                status_code=denied.status_code,
                content={"detail": denied.detail},
                headers=denied.headers,
            )
        return await call_next(request)
