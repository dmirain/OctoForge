"""Operator console API exposed as one router over resource-focused internals."""

from fastapi import APIRouter, Depends
from octoforge_server.deps import require_admin

from .cron import router as cron_router
from .data import router as data_router
from .instructions import router as instructions_router
from .overview import router as overview_router
from .params import router as params_router
from .settings import router as settings_router
from .tariffs import router as tariffs_router
from .tasks import router as tasks_router
from .usage import router as usage_router
from .user_status import router as user_status_router
from .users import router as users_router

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

for resource_router in (
    overview_router,
    tasks_router,
    cron_router,
    instructions_router,
    data_router,
    params_router,
    tariffs_router,
    usage_router,
    users_router,
    user_status_router,
    settings_router,
):
    router.include_router(resource_router)
