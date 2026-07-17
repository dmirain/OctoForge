"""Basic skill writing records into user-owned datasets (create-if-absent)."""

from typing import Any

from octoforge_core.datasets.api import (
    Dataset,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetRecordValidationError,
    DatasetSchema,
    DatasetSchemaError,
    DatasetService,
)
from octoforge_core.datasets.validation import parse_schema, validate_record
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "data_put"
SKILL_DESCRIPTION = (
    "Write a record into one of the user's datasets (food/weight/habit trackers and "
    "the like). If the dataset does not exist yet it is created on the fly — then "
    "'schema' ({'fields': [{'name', 'type', 'required?'}]}) and 'description' are "
    "required. The record is validated against the dataset schema."
)
CREATION_HINT = (
    "dataset '{name}' does not exist and will be created: "
    "'schema' (object with a 'fields' list) and 'description' (string) are required"
)
CREATED_TEMPLATE = "dataset '{name}' created; record {record_id} added at {created_at}"
ADDED_TEMPLATE = "record {record_id} added to dataset '{name}' at {created_at}"
PARAMETERS_SCHEMA: dict[str, Any] = {
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


class DataPutSkill:
    """Thin adapter over the DatasetService facade."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, create the dataset if absent and append the record."""
        name = _dataset_name(arguments.get("dataset"))
        record = _record_payload(arguments.get("record"))
        dataset, created = await self._ensure_dataset(arguments, context.user_id, name)
        try:
            validate_record(dataset.schema, record)
        except DatasetRecordValidationError as exc:
            raise SkillArgumentsError(
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
            raise SkillArgumentsError(CREATION_HINT.format(name=name))
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


def _dataset_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError("dataset must be a non-empty string")
    return raw


def _record_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SkillArgumentsError("record must be an object")
    return raw


def _creation_schema(raw: object) -> DatasetSchema | None:
    if raw is None:
        return None
    try:
        return parse_schema(raw)
    except DatasetSchemaError as exc:
        raise SkillArgumentsError(f"invalid schema: {exc}") from exc


def _optional_text(raw: object, argument: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError(f"{argument} must be a non-empty string")
    return raw
