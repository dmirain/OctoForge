"""Shared console response shapes and dialog-cleanup dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, TypeVar

from fastapi import Depends
from octoforge_core.admin.api import Page
from octoforge_core.context.api import SummaryStore
from octoforge_core.dialogs.api import ClaimRepository, ExchangeRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.tasks.store import TaskStore

from .deps import ClaimsDep, ExchangesDep, SummariesDep, TaskStoreDep

DELETED_STATUS = {"status": "deleted"}


@dataclass(frozen=True, slots=True)
class DialogCascade:
    """Per-module deleters composed by the dialog-deletion endpoint."""

    tasks: TaskStore
    summaries: SummaryStore
    exchanges: ExchangeRepository
    claims: ClaimRepository


@dataclass(frozen=True, slots=True)
class _DialogContentStores:
    tasks: TaskStoreDep
    summaries: SummariesDep


@dataclass(frozen=True, slots=True)
class _DialogStateStores:
    exchanges: ExchangesDep
    claims: ClaimsDep


_ContentStoresDep = Annotated[_DialogContentStores, Depends()]
_StateStoresDep = Annotated[_DialogStateStores, Depends()]


def dialog_cascade(content: _ContentStoresDep, state: _StateStoresDep) -> DialogCascade:
    return DialogCascade(content.tasks, content.summaries, state.exchanges, state.claims)


DialogCascadeDep = Annotated[DialogCascade, Depends(dialog_cascade)]
T = TypeVar("T")


def page_payload(page: Page[T], to_dict: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    """Wire shape of a listing: items plus what the pager needs."""
    return {
        "items": [to_dict(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


async def names(identities: IdentityStore) -> dict[str, str]:
    """Resolve canonical names once for an entire listing."""
    return {user.id: user.name for user in await identities.list_users() if user.name}


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
