"""The HTTP service: the application, its guard, its probes, and nothing else.

This is what serves core over HTTP. It knows how to authenticate a request,
how to answer whether it is alive, and how to mount whatever it is handed —
but not what a Telegram bot is, nor how a database engine is opened. Both of
those belong to the deployment above it (`main.py`), which passes in the
runtime and the routes.

The separation is what makes an interface optional. As long as the thing
building the application also knows how to build a bot, a deployment without
one means editing this file.

The service is not meant to face the open internet: it answers the interfaces
installed in front of it. It is still guarded — one credential for the
operator, a narrower one for a relay — because "internal" is a deployment
promise, not a property of the code.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_server.api.cron import router as cron_router
from octoforge_server.api.dialog import router as dialog_router
from octoforge_server.api.media import router as media_router
from octoforge_server.api.secrets import router as secrets_router
from octoforge_server.auth import (
    CROSS_SITE_MESSAGE,
    AuthGate,
    allows_service_credential,
    is_cross_site_mutation,
    is_open_path,
)
from octoforge_server.config import Settings
from octoforge_server.runtime_state import Runtime
from octoforge_server.surfaces import StaticFile, SurfaceRoutes

logger = logging.getLogger(__name__)

#: The service's own pages. Not a surface: the secrets page is how a secret
#: gets filled in, not an interface anyone chooses to install.
STATIC_DIR = Path(__file__).parent / "static"
SECRETS_PAGE = StaticFile(path="/secrets.html", file=STATIC_DIR / "secrets.html")

APP_TITLE = "OctoForge"
HEALTH_STATUS = "ok"
READY_STATUS = "ready"
NOT_READY_STATUS = "not-ready"

# Signature of the next handler in Starlette's middleware chain.
NextCall = Callable[[Request], Awaitable[Response]]

#: How the deployment hands over its assembled services.
RuntimeFactory = Callable[[Settings], AbstractAsyncContextManager[Runtime]]


def build_app(
    settings: Settings,
    runtime_factory: RuntimeFactory,
    routes: Sequence[SurfaceRoutes] = (),
) -> FastAPI:
    """Build the service, mounting whatever the deployment installed on it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with runtime_factory(settings) as rt:
            for name, value in rt.as_state().items():
                setattr(app.state, name, value)
            yield

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
    _install_guard(app, settings)
    _install_probes(app)
    # the service's own endpoints, present in every deployment
    app.include_router(dialog_router)
    app.include_router(cron_router)
    app.include_router(media_router)
    app.include_router(secrets_router)
    for surface in routes:
        for router in surface.routers:
            app.include_router(router)
        for item in surface.static_files:
            serve_file(app, item)
    return app


def _install_guard(app: FastAPI, settings: Settings) -> None:
    """Put the credential check in front of everything but the health probes."""
    gate = AuthGate(
        username=settings.admin_username,
        password_hash=settings.admin_password_hash,
        service_username=settings.service_username,
        service_password_hash=settings.service_password_hash,
    )
    # the admin router's own dependency resolves the same object, so a request
    # verified by the middleware does not hash again
    app.state.auth_gate = gate

    @app.middleware("http")
    async def authenticate(request: Request, call_next: NextCall) -> Response:
        """Require a credential for everything but the health probes.

        A middleware rather than per-router dependencies because it has to cover
        what routers do not: static pages, `/docs` and `/openapi.json`.
        Exceptions raised here bypass the app's handlers, so the responses are
        built by hand.

        Two checks, in order. The first refuses state-changing requests a
        browser sends from another site: with Basic auth the credential rides
        along automatically, so a form on an attacker's page would otherwise
        act as the operator. The second is the credential itself, and which
        credentials are acceptable is decided by the path.
        """
        if is_cross_site_mutation(request):
            logger.warning("cross-site %s to %s refused", request.method, request.url.path)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": CROSS_SITE_MESSAGE},
            )
        if not is_open_path(request.url.path):
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


def _install_probes(app: FastAPI) -> None:
    """Liveness and readiness, the two paths served without a credential."""

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": HEALTH_STATUS}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, str]:
        """Readiness probe: verify the database answers a trivial query."""
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.warning("readiness check failed: database unavailable", exc_info=True)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": NOT_READY_STATUS, "database": "down"}
        return {"status": READY_STATUS, "database": "ok"}


def serve_file(app: FastAPI, item: StaticFile) -> None:
    """Serve one file at one URL.

    Per file rather than one mounted directory: several surfaces serve from
    the same root, and two directories cannot both be mounted at `/`.
    """

    async def handler() -> FileResponse:
        return FileResponse(item.file)

    app.get(item.path, include_in_schema=False)(handler)
