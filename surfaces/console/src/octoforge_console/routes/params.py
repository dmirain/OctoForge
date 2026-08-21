"""Per-user parameter and secret-metadata administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import clamp_page
from octoforge_core.params.api import InvalidUserParamError, UserParamNotFoundError
from octoforge_server import audit
from pydantic import BaseModel

from .actions import ListingServicesDep, ParamActionDep
from .common import DELETED_STATUS, names, page_payload
from .data_serializers import secret as serialize_secret
from .data_serializers import user_param as serialize_user_param
from .deps import OperatorDep, UserParamsDep
from .query import PageDep

router = APIRouter()


class SetUserParamRequest(BaseModel):
    user_id: str
    code: str
    value: str


@router.get("/params")
async def user_params(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_user_params(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_user_param(item, resolved_names))


@router.post("/params")
async def set_user_param(
    request: SetUserParamRequest, operator: OperatorDep, store: UserParamsDep
) -> dict[str, Any]:
    try:
        param = await store.put(request.user_id, request.code, request.value)
    except InvalidUserParamError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("param.set", operator, f"{request.user_id}/{param.code}"))
    return {"user_id": request.user_id, "code": param.code, "value": param.value}


@router.delete("/params/{code}")
async def delete_user_param(code: str, user_id: str, action: ParamActionDep) -> dict[str, str]:
    try:
        await action.store.delete(user_id, code)
    except UserParamNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidUserParamError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("param.delete", action.operator, f"{user_id}/{code}"))
    return DELETED_STATUS


@router.get("/secrets")
async def secrets(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_secrets(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_secret(item, resolved_names))
