"""Typed filtered listing requests for the admin read model."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class TaskListing:
    limit: int
    offset: int
    status: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeListing:
    limit: int
    offset: int
    user_id: str | None = None
    status: str | None = None
