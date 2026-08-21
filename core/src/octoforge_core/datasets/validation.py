"""Dataset schema parsing and record validation."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from octoforge_core.datasets.api import (
    DatasetField,
    DatasetRecordValidationError,
    DatasetSchema,
    DatasetSchemaError,
    FieldType,
)

FIELDS_KEY = "fields"
NAME_KEY = "name"
TYPE_KEY = "type"
REQUIRED_KEY = "required"


def parse_schema(raw: object) -> DatasetSchema:
    """Parse the JSON schema document into a DatasetSchema.

    Raises `DatasetSchemaError` with a human-readable reason when the shape is
    wrong: not an object, missing/invalid fields, duplicated names, unknown type.
    """
    if not isinstance(raw, dict):
        raise DatasetSchemaError("schema must be an object with a 'fields' list")
    fields_raw = raw.get(FIELDS_KEY)
    if not isinstance(fields_raw, list):
        raise DatasetSchemaError("schema must contain a 'fields' list")
    fields = tuple(_parse_field(index, entry) for index, entry in enumerate(fields_raw))
    names = [field.name for field in fields]
    if len(set(names)) != len(names):
        raise DatasetSchemaError("schema field names must be unique")
    return DatasetSchema(fields=fields)


def dump_schema(schema: DatasetSchema) -> dict[str, Any]:
    """Serialize a DatasetSchema back into the JSON schema document format."""
    return {
        FIELDS_KEY: [
            {NAME_KEY: field.name, TYPE_KEY: field.type.value, REQUIRED_KEY: field.required}
            for field in schema.fields
        ]
    }


def validate_record(schema: DatasetSchema, payload: dict[str, Any]) -> None:
    """Check a record payload against the dataset schema.

    Extra payload fields beyond the schema are allowed (document storage).
    A missing or null value violates a required field; an optional field may
    be absent or null. Raises `DatasetRecordValidationError` listing every
    violation found.
    """
    violations: list[str] = []
    for field in schema.fields:
        value = payload.get(field.name)
        if value is None:
            if field.required:
                violations.append(f"field '{field.name}' is required")
            continue
        if not _matches_type(field.type, value):
            violations.append(
                f"field '{field.name}' must be {field.type.value}, got {type(value).__name__}"
            )
    if violations:
        raise DatasetRecordValidationError(violations)


def _parse_field(index: int, entry: object) -> DatasetField:
    if not isinstance(entry, dict):
        raise DatasetSchemaError(f"field #{index} must be an object")
    name = entry.get(NAME_KEY)
    if not isinstance(name, str) or not name.strip():
        raise DatasetSchemaError(f"field #{index} must have a non-empty string 'name'")
    field_type = _parse_field_type(name, entry.get(TYPE_KEY))
    required = entry.get(REQUIRED_KEY, False)
    if not isinstance(required, bool):
        raise DatasetSchemaError(f"field '{name}': 'required' must be a boolean")
    return DatasetField(name=name, type=field_type, required=required)


def _parse_field_type(name: str, raw: object) -> FieldType:
    try:
        return FieldType(str(raw))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in FieldType)
        raise DatasetSchemaError(
            f"field '{name}': unknown type {raw!r} (allowed: {allowed})"
        ) from exc


def _matches_type(field_type: FieldType, value: object) -> bool:
    match field_type:
        case FieldType.STRING:
            return isinstance(value, str)
        case FieldType.INTEGER:
            # bool is an int subclass in Python but is not an integer here
            return isinstance(value, int) and not isinstance(value, bool)
        case FieldType.NUMBER:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case FieldType.BOOLEAN:
            return isinstance(value, bool)
        case FieldType.DATE:
            return isinstance(value, str) and _parses(value, date.fromisoformat)
        case FieldType.DATETIME:
            return isinstance(value, str) and _parses(value, datetime.fromisoformat)


def _parses(text: str, parser: Callable[[str], object]) -> bool:
    try:
        parser(text)
    except ValueError:
        return False
    return True
