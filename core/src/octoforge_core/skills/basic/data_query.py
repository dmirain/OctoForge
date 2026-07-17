"""Basic skill querying records of user-owned datasets."""

import json
from datetime import UTC, date, datetime, time
from typing import Any

from octoforge_core.datasets.api import DatasetNotFoundError, DatasetRecord, DatasetService
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "data_query"
SKILL_DESCRIPTION = (
    "Query records of one of the user's datasets: equality filter on payload fields, "
    "created_at date range (ISO strings; a date-only value means the whole day, UTC) "
    "and a limit. Records come back newest first as JSON lines. Aggregation for "
    "reports is done by the model over the returned sample."
)
NOT_FOUND_TEMPLATE = "dataset '{name}' not found"
NO_RECORDS_TEMPLATE = "no records in dataset '{name}'"
HEADER_TEMPLATE = "{count} record(s) in dataset '{name}' (newest first):"
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset": {"type": "string", "description": "Dataset name"},
        "equals": {
            "type": "object",
            "description": "Payload field equality filter (type-sensitive)",
        },
        "date_from": {
            "type": "string",
            "description": "ISO date/datetime; records created at or after (UTC)",
        },
        "date_to": {
            "type": "string",
            "description": "ISO date/datetime; records created at or before (UTC)",
        },
        "limit": {"type": "integer", "description": "Max records to return"},
    },
    "required": ["dataset"],
}


class DataQuerySkill:
    """Thin adapter over the DatasetService facade."""

    def __init__(self, service: DatasetService, default_limit: int, max_limit: int) -> None:
        self._service = service
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, query and format the records as JSON lines."""
        name = _dataset_name(arguments.get("dataset"))
        equals = _equals_filter(arguments.get("equals"))
        date_from = _date_boundary(arguments.get("date_from"), "date_from", end_of_day=False)
        date_to = _date_boundary(arguments.get("date_to"), "date_to", end_of_day=True)
        limit = self._limit(arguments.get("limit"))
        try:
            records = await self._service.query_records(
                context.user_id, name, equals, date_from, date_to, limit
            )
        except DatasetNotFoundError:
            return NOT_FOUND_TEMPLATE.format(name=name)
        if not records:
            return NO_RECORDS_TEMPLATE.format(name=name)
        lines = [HEADER_TEMPLATE.format(count=len(records), name=name)]
        lines.extend(_record_line(record) for record in records)
        return "\n".join(lines)

    def _limit(self, raw: object) -> int:
        if raw is None:
            return self._default_limit
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SkillArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise SkillArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw


def _dataset_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError("dataset must be a non-empty string")
    return raw


def _equals_filter(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SkillArgumentsError("equals must be an object with string keys")
    return raw


def _date_boundary(raw: object, argument: str, *, end_of_day: bool) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError(f"{argument} must be an ISO date or datetime string")
    text = raw.strip()
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        # a date-only value spans the whole UTC day
        boundary_time = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SkillArgumentsError(f"{argument} must be an ISO date or datetime string") from exc
    if parsed.tzinfo is None:
        # naive datetimes are read as UTC (UTC everywhere convention)
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record_line(record: DatasetRecord) -> str:
    return json.dumps(
        {
            "id": record.id,
            "created_at": record.created_at.isoformat(),
            "payload": record.payload,
        },
        ensure_ascii=False,
    )
