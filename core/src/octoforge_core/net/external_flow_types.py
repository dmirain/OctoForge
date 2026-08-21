"""Typed requests passed between endpoint response handlers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from octoforge_core.net.collections.ingest import CollectionSink
from octoforge_core.net.external_types import (
    CallOptions,
    ExternalCallContext,
    ExternalCallResult,
    ExternalPage,
)
from octoforge_core.net.spec_types import PaginationSpec, ResponseSpec, ToolSpec

PageLoader = Callable[[ToolSpec, dict[str, str], str | None], Awaitable[ExternalPage]]


@dataclass(frozen=True, slots=True)
class SpillRequest:
    name: str
    user_id: str | None
    scope: str
    response: ResponseSpec | None


@dataclass(frozen=True, slots=True)
class CollectionCall:
    name: str
    spec: ToolSpec
    validated: dict[str, str]
    user_id: str | None
    options: CallOptions


@dataclass(frozen=True, slots=True)
class PourCall:
    request: CollectionCall
    page: ExternalPage


@dataclass(frozen=True, slots=True)
class DelegateCall:
    name: str
    kind: str
    content: str
    params: dict[str, object]
    context: ExternalCallContext


@dataclass(slots=True)
class CollectionProgress:
    status: int = 0
    pages: int = 0
    capped: bool = False


@dataclass(frozen=True, slots=True)
class CollectionWalk:
    call: CollectionCall
    sink: CollectionSink
    pagination: PaginationSpec
    limit: int


@dataclass(frozen=True, slots=True)
class WalkOutcome:
    progress: CollectionProgress
    early: ExternalCallResult | None = None
