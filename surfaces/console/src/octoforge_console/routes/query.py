"""Typed query groups shared by console listing endpoints."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from .deps import LimitDep, OffsetDep


@dataclass(frozen=True, slots=True)
class PageQuery:
    limit: LimitDep = None
    offset: OffsetDep = None


@dataclass(frozen=True, slots=True)
class TaskFilter:
    status: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeFilter:
    user_id: str | None = None
    status: str | None = None


PageDep = Annotated[PageQuery, Depends()]
TaskFilterDep = Annotated[TaskFilter, Depends()]
ExchangeFilterDep = Annotated[ExchangeFilter, Depends()]
