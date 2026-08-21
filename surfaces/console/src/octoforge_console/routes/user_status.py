"""Operator-controlled membership transitions."""

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from octoforge_core.cron.api import CronEnablement
from octoforge_core.identity.api import UserNotFoundError, UserStatus
from octoforge_server import audit

from .actions import UserStatusActionDep

router = APIRouter()


@router.post("/users/{user_id}/status")
async def set_user_status(
    user_id: str,
    status: Annotated[UserStatus, Query()],
    action: UserStatusActionDep,
) -> dict[str, Any]:
    identities = action.identity.identities
    try:
        before = (await identities.get_user(user_id)).status
        await identities.set_status(user_id, status)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    paused = 0
    if status is UserStatus.BANNED:
        for job in await action.cron_store.list_for_user(user_id):
            if job.enabled:
                await action.cron_store.set_enabled(CronEnablement(user_id, job.id, False))
                paused += 1
    notified = False
    if status is UserStatus.ACTIVE and before is UserStatus.WAITING and action.notifier is not None:
        notified = await action.notifier(user_id)
    audit.record(
        audit.AuditEvent("user.status", action.identity.operator, f"{user_id}/{status.value}")
    )
    return {
        "user_id": user_id,
        "status": status.value,
        "paused_cron_jobs": paused,
        "notified": notified,
    }
