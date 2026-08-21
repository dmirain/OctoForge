"""Plan catalog and assignment administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.tariffs.api import (
    InvalidTariffError,
    TariffDefinition,
    TariffInUseError,
    TariffLimits,
    TariffNotFoundError,
)
from octoforge_server import audit

from .actions import TariffActionDep
from .common import DELETED_STATUS, names
from .deps import IdentityStoreDep, KnownFeaturesDep, OperatorDep, TariffStoreDep
from .tariff_models import AssignTariffRequest, SetTariffRequest
from .tariff_serializers import tariff as serialize_tariff

router = APIRouter()


@router.get("/tariffs")
async def tariffs(
    store: TariffStoreDep, identities: IdentityStoreDep, known_features: KnownFeaturesDep
) -> dict[str, Any]:
    plans = await store.list()
    assignments = await store.assignments()
    resolved_names = await names(identities)
    users_of: dict[str, list[str]] = {}
    for user_id, code in sorted(assignments.items()):
        users_of.setdefault(code, []).append(resolved_names.get(user_id) or user_id)
    items = [serialize_tariff(item) | {"users": users_of.get(item.code, [])} for item in plans]
    return {
        "items": items,
        "total": len(items),
        "limit": max(1, len(items)),
        "offset": 0,
        "features": sorted(known_features),
    }


@router.post("/tariffs")
async def set_tariff(
    request: SetTariffRequest,
    action: TariffActionDep,
) -> dict[str, Any]:
    catalog = action.catalog
    known_features = catalog.known_features
    unknown = sorted(set(request.features) - known_features)
    if unknown:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=(
                f"unknown feature(s): {', '.join(unknown)}; "
                f"known: {', '.join(sorted(known_features))}"
            ),
        )
    limits = TariffLimits(
        daily_tokens=request.daily_tokens,
        daily_user_messages=request.daily_user_messages,
        daily_assistant_messages=request.daily_assistant_messages,
        max_cron_jobs=request.max_cron_jobs,
        max_datasets=request.max_datasets,
        max_memory_chars=request.max_memory_chars,
    )
    try:
        tariff = await catalog.store.put(
            TariffDefinition(
                request.code,
                request.title,
                frozenset(request.features),
                limits,
                is_default=request.is_default,
            )
        )
    except InvalidTariffError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("tariff.set", action.operator, tariff.code))
    return serialize_tariff(tariff)


@router.delete("/tariffs/{code}")
async def delete_tariff(code: str, operator: OperatorDep, store: TariffStoreDep) -> dict[str, str]:
    try:
        await store.delete(code)
    except TariffNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except TariffInUseError as exc:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
    except InvalidTariffError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("tariff.delete", operator, code))
    return DELETED_STATUS


@router.post("/tariffs/assign")
async def assign_tariff(
    request: AssignTariffRequest, operator: OperatorDep, store: TariffStoreDep
) -> dict[str, Any]:
    try:
        await store.assign(request.user_id, request.code)
    except TariffNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except InvalidTariffError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
    audit.record(
        audit.AuditEvent("tariff.assign", operator, f"{request.user_id}/{request.code or '-'}")
    )
    return {"user_id": request.user_id, "code": request.code}
