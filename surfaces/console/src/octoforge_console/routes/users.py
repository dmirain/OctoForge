"""Person-centric installation membership view."""

from typing import Any

from fastapi import APIRouter
from octoforge_core.identity.api import UserStatus
from octoforge_core.settings.api import max_active_users

from .deps import IdentityStoreDep, SettingsStoreDep, TariffStoreDep

router = APIRouter()


@router.get("/users")
async def users(
    identities: IdentityStoreDep,
    settings_store: SettingsStoreDep,
    store: TariffStoreDep,
) -> dict[str, Any]:
    people = await identities.list_users()
    counts = await identities.count_by_status()
    plans = await store.list()
    assignments = await store.assignments()
    default_code = next((plan.code for plan in plans if plan.is_default), None)
    items = [
        {
            "user_id": person.id,
            "name": person.name,
            "email": person.email,
            "status": person.status.value,
            "tariff": assignments.get(person.id, default_code),
            "tariff_assigned": person.id in assignments,
            "created_at": person.created_at.isoformat() if person.created_at else None,
            "identities": [
                {
                    "surface": item.surface,
                    "external_id": item.external_id,
                    "name": item.name,
                    "username": item.username,
                    "active": item.active,
                }
                for item in await identities.identities_of(person.id)
            ],
        }
        for person in people
    ]
    return {
        "items": items,
        "total": len(items),
        "limit": max(1, len(items)),
        "offset": 0,
        "active_count": counts[UserStatus.ACTIVE],
        "waiting_count": counts[UserStatus.WAITING],
        "banned_count": counts[UserStatus.BANNED],
        "max_active_users": await max_active_users(settings_store),
        "tariffs": [plan.code for plan in plans],
        "default_tariff": default_code,
    }
