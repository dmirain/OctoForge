"""Installation setting administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.settings.api import InvalidSettingError
from octoforge_server import audit
from pydantic import BaseModel

from .common import DELETED_STATUS, iso
from .deps import OperatorDep, SettingsStoreDep

router = APIRouter()


class SetSettingRequest(BaseModel):
    key: str
    value: str


@router.get("/settings")
async def settings_list(settings_store: SettingsStoreDep) -> dict[str, Any]:
    items = [
        {"key": item.key, "value": item.value, "updated_at": iso(item.updated_at)}
        for item in await settings_store.list()
    ]
    return {"items": items, "total": len(items), "limit": max(1, len(items)), "offset": 0}


@router.post("/settings")
async def set_setting(
    request: SetSettingRequest, operator: OperatorDep, settings_store: SettingsStoreDep
) -> dict[str, Any]:
    try:
        stored = await settings_store.put(request.key, request.value)
    except InvalidSettingError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("setting.set", operator, stored.key))
    return {"key": stored.key, "value": stored.value}


@router.delete("/settings/{key}")
async def delete_setting(
    key: str, operator: OperatorDep, settings_store: SettingsStoreDep
) -> dict[str, str]:
    try:
        await settings_store.delete(key)
    except InvalidSettingError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("setting.delete", operator, key))
    return DELETED_STATUS
