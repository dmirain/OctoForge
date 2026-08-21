"""The data_put tool and its creation-time argument policy."""

from typing import Any

from octoforge_core.datasets.requests import DatasetDefinition
from octoforge_core.datasets.service_port import DatasetService
from octoforge_core.datasets.tool_contract import (
    ADDED_TEMPLATE,
    CREATED_TEMPLATE,
    CREATION_HINT,
    PUT_DESCRIPTION,
    PUT_NAME,
    PUT_SCHEMA,
)
from octoforge_core.datasets.types import (
    Dataset,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetRecordValidationError,
    DatasetSchema,
    DatasetSchemaError,
)
from octoforge_core.datasets.validation import parse_schema, validate_record
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class DataPutTool:
    """Create a dataset when needed, validate one record and append it."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=PUT_NAME, description=PUT_DESCRIPTION, parameters_schema=PUT_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
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
        try:
            return await self._service.get_dataset(user_id, name), False
        except DatasetNotFoundError:
            pass
        definition = _creation_definition(arguments, user_id, name)
        try:
            dataset = await self._service.create_dataset(definition)
        except DatasetExistsError:
            dataset = await self._service.get_dataset(user_id, name)
        return dataset, True


def _creation_definition(
    arguments: dict[str, Any],
    user_id: str,
    name: str,
) -> DatasetDefinition:
    schema = _creation_schema(arguments.get("schema"))
    description = _optional_text(arguments.get("description"), "description")
    if schema is None or description is None:
        raise ToolArgumentsError(CREATION_HINT.format(name=name))
    return DatasetDefinition(
        user_id,
        name,
        description,
        schema,
        _optional_text(arguments.get("usage_notes"), "usage_notes") or "",
        _optional_text(arguments.get("retention"), "retention") or "",
    )


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
