"""Recall tool orchestration over instruction and dataset search."""

import asyncio
from typing import Any

from octoforge_core.datasets.api import DatasetHit, DatasetService
from octoforge_core.instructions._search_spec import (
    MAX_K,
    SEARCH_DESCRIPTION,
    SEARCH_NAME,
    SEARCH_SCHEMA,
)
from octoforge_core.instructions._search_tool_format import render_search
from octoforge_core.instructions.ports import InstructionService
from octoforge_core.instructions.requests import InstructionSearchRequest
from octoforge_core.instructions.types import InstructionType
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class InstructionSearchTool:
    """Validate recall, run independent stores concurrently, and render their hits."""

    def __init__(
        self,
        service: InstructionService,
        default_k: int,
        datasets: DatasetService | None = None,
    ) -> None:
        self._service = service
        self._default_k = default_k
        self._datasets = datasets

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(SEARCH_NAME, SEARCH_DESCRIPTION, SEARCH_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentsError("query must be a non-empty string")
        limit = self._parse_limit(arguments.get("k"))
        kind = _parse_optional_kind(arguments.get("type"))
        hits, dataset_hits = await asyncio.gather(
            self._service.search(
                context.user_id,
                InstructionSearchRequest(query, limit, kind),
            ),
            self._dataset_hits(context.user_id, query, limit),
        )
        return render_search(hits, dataset_hits, limit)

    async def _dataset_hits(self, user_id: str, query: str, limit: int) -> list[DatasetHit]:
        if self._datasets is None:
            return []
        return await self._datasets.search(user_id, query, limit)

    def _parse_limit(self, raw: object) -> int:
        if raw is None:
            return self._default_k
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentsError("k must be an integer")
        if raw < 1 or raw > MAX_K:
            raise ToolArgumentsError(f"k must be between 1 and {MAX_K}")
        return raw


def _parse_optional_kind(raw: object) -> InstructionType | None:
    if raw is None:
        return None
    try:
        return InstructionType(str(raw))
    except ValueError as exc:
        raise ToolArgumentsError(f"unsupported instruction type: {raw!r}") from exc
