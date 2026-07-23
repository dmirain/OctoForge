"""Tools of the datasets module: data_put/data_query/data_forget over the facade."""

import json
from datetime import UTC, date, datetime, time
from typing import Any

from octoforge_core.datasets.api import (
    Dataset,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetRecordValidationError,
    DatasetSchema,
    DatasetSchemaError,
    DatasetService,
)
from octoforge_core.datasets.validation import parse_schema, validate_record
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

PUT_NAME = "data_put"
PUT_DESCRIPTION = (
    "Write a record into one of the user's datasets — structured list-like data the "
    "user tracks over time (food/weight/habit trackers and the like; e.g. today's "
    "intake 'k2000 p100 f60 c120' goes to a food-log dataset with numeric fields). "
    "If the dataset does not exist yet it is created on the fly — then "
    "'schema' ({'fields': [{'name', 'type', 'required?'}]}) and 'description' are "
    "required. The record is validated against the dataset schema."
)
CREATION_HINT = (
    "dataset '{name}' does not exist and will be created: "
    "'schema' (object with a 'fields' list) and 'description' (string) are required"
)
CREATED_TEMPLATE = "dataset '{name}' created; record {record_id} added at {created_at}"
ADDED_TEMPLATE = "record {record_id} added to dataset '{name}' at {created_at}"
PUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset": {"type": "string", "description": "Dataset name"},
        "record": {
            "type": "object",
            "description": "Record payload matching the dataset schema",
        },
        "description": {
            "type": "string",
            "description": "Dataset purpose (creation only)",
        },
        "schema": {
            "type": "object",
            "description": "Dataset schema {'fields': [...]} (creation only)",
        },
        "usage_notes": {
            "type": "string",
            "description": "How to write/read/aggregate the data (creation only)",
        },
        "retention": {
            "type": "string",
            "description": "Retention policy (creation only)",
        },
    },
    "required": ["dataset", "record"],
}

QUERY_NAME = "data_query"
QUERY_DESCRIPTION = (
    "Query records of one of the user's datasets: equality filter on payload fields, "
    "created_at date range (ISO strings; a date-only value means the whole day, UTC) "
    "and a limit. Records come back newest first as JSON lines. Aggregation for "
    "reports is done by the model over the returned sample."
)
NOT_FOUND_TEMPLATE = "dataset '{name}' not found"
NO_RECORDS_TEMPLATE = "no records in dataset '{name}'"
HEADER_TEMPLATE = "{count} record(s) in dataset '{name}' (newest first):"
QUERY_SCHEMA: dict[str, Any] = {
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

FORGET_NAME = "data_forget"
FORGET_DESCRIPTION = (
    "Delete one of the user's datasets with all its records. "
    "Use it when the user asks to forget everything about a tracked topic."
)
DELETED_TEMPLATE = "dataset '{name}' deleted with {count} record(s)"
FORGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset": {"type": "string", "description": "Dataset name"},
    },
    "required": ["dataset"],
}


class DataPutTool:
    """Thin adapter over the DatasetService facade."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=PUT_NAME,
            description=PUT_DESCRIPTION,
            parameters_schema=PUT_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, create the dataset if absent and append the record."""
        name = _dataset_name(arguments.get("dataset"))
        record = _record_payload(arguments.get("record"))
        dataset, created = await self._ensure_dataset(arguments, context.user_id, name)
        try:
            validate_record(dataset.schema, record)
        except DatasetRecordValidationError as exc:
            raise ToolArgumentsError(
                f"record does not match the schema of dataset '{name}': "
                + "; ".join(exc.violations)
            ) from exc
        added = await self._service.add_record(context.user_id, name, record)
        template = CREATED_TEMPLATE if created else ADDED_TEMPLATE
        return template.format(
            name=name,
            record_id=added.id,
            created_at=added.created_at.isoformat(),
        )

    async def _ensure_dataset(
        self,
        arguments: dict[str, Any],
        user_id: str,
        name: str,
    ) -> tuple[Dataset, bool]:
        """Return the dataset and whether this call created it."""
        try:
            return await self._service.get_dataset(user_id, name), False
        except DatasetNotFoundError:
            pass
        schema = _creation_schema(arguments.get("schema"))
        description = _optional_text(arguments.get("description"), "description")
        if schema is None or description is None:
            raise ToolArgumentsError(CREATION_HINT.format(name=name))
        usage_notes = _optional_text(arguments.get("usage_notes"), "usage_notes") or ""
        retention = _optional_text(arguments.get("retention"), "retention") or ""
        try:
            dataset = await self._service.create_dataset(
                user_id, name, description, schema, usage_notes, retention
            )
        except DatasetExistsError:
            # a concurrent writer created the dataset after our get_dataset miss
            dataset = await self._service.get_dataset(user_id, name)
        return dataset, True


class DataQueryTool:
    """Thin adapter over the DatasetService facade."""

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
            raise ToolArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise ToolArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw


class DataForgetTool:
    """Thin adapter over the DatasetService facade."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=FORGET_NAME,
            description=FORGET_DESCRIPTION,
            parameters_schema=FORGET_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, delete the dataset and report the record count."""
        name = arguments.get("dataset")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("dataset must be a non-empty string")
        try:
            records_count = await self._service.delete_dataset(context.user_id, name)
        except DatasetNotFoundError:
            return NOT_FOUND_TEMPLATE.format(name=name)
        return DELETED_TEMPLATE.format(name=name, count=records_count)


def _dataset_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("dataset must be a non-empty string")
    return raw


def _record_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ToolArgumentsError("record must be an object")
    return raw


def _creation_schema(raw: object) -> DatasetSchema | None:
    if raw is None:
        return None
    try:
        return parse_schema(raw)
    except DatasetSchemaError as exc:
        raise ToolArgumentsError(f"invalid schema: {exc}") from exc


def _optional_text(raw: object, argument: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be a non-empty string")
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
        # a date-only value spans the whole UTC day
        boundary_time = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolArgumentsError(f"{argument} must be an ISO date or datetime string") from exc
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
