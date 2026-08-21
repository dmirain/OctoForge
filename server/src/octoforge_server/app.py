"""The HTTP service, probes and installed routes, independent of deployment wiring."""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_server.api.cron import router as cron_router
from octoforge_server.api.dialog import router as dialog_router
from octoforge_server.api.identity import router as identity_router
from octoforge_server.api.media import router as media_router
from octoforge_server.api.secrets import router as secrets_router
from octoforge_server.config import Settings
from octoforge_server.request_guard import install_guard
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
    install_guard(app, settings)
    _install_probes(app)
    # the service's own endpoints, present in every deployment
    app.include_router(dialog_router)
    app.include_router(cron_router)
    app.include_router(media_router)
    app.include_router(identity_router)
    app.include_router(secrets_router)
    for surface in routes:
        for router in surface.routers:
            app.include_router(router)
        for item in surface.static_files:
            serve_file(app, item)
    return app


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
