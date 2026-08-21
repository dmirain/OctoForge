"""The data_query tool and its record-filter argument policy."""

import json
from datetime import UTC, date, datetime, time
from typing import Any

from octoforge_core.datasets.requests import DatasetRecordQuery
from octoforge_core.datasets.service_port import DatasetService
from octoforge_core.datasets.tool_contract import (
    HEADER_TEMPLATE,
    NO_RECORDS_TEMPLATE,
    NOT_FOUND_TEMPLATE,
    QUERY_DESCRIPTION,
    QUERY_NAME,
    QUERY_SCHEMA,
)
from octoforge_core.datasets.types import DatasetNotFoundError, DatasetRecord
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class DataQueryTool:
    """Validate record filters, execute the query and render JSON lines."""

    def __init__(self, service: DatasetService, default_limit: int, max_limit: int) -> None:
        self._service = service
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=QUERY_NAME,
            description=QUERY_DESCRIPTION,
            parameters_schema=QUERY_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        request = DatasetRecordQuery(
            context.user_id,
            _dataset_name(arguments.get("dataset")),
            _equals_filter(arguments.get("equals")),
            _date_boundary(arguments.get("date_from"), "date_from", end_of_day=False),
            _date_boundary(arguments.get("date_to"), "date_to", end_of_day=True),
            self._limit(arguments.get("limit")),
        )
        try:
            records = await self._service.query_records(request)
        except DatasetNotFoundError:
            return NOT_FOUND_TEMPLATE.format(name=request.dataset_name)
        if not records:
            return NO_RECORDS_TEMPLATE.format(name=request.dataset_name)
        lines = [HEADER_TEMPLATE.format(count=len(records), name=request.dataset_name)]
        lines.extend(_record_line(record) for record in records)
        return "\n".join(lines)

    def _limit(self, raw: object) -> int:
        if raw is None:
            return self._default_limit
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise ToolArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw


def _dataset_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("dataset must be a non-empty string")
    return raw


def _equals_filter(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ToolArgumentsError("equals must be an object with string keys")
    return raw


def _date_boundary(raw: object, argument: str, *, end_of_day: bool) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be an ISO date or datetime string")
    text = raw.strip()
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        boundary_time = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolArgumentsError(f"{argument} must be an ISO date or datetime string") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record_line(record: DatasetRecord) -> str:
    return json.dumps(
        {"id": record.id, "created_at": record.created_at.isoformat(), "payload": record.payload},
        ensure_ascii=False,
    )
