"""Tests for the datasets module schema parsing and record validation."""

from typing import Any

import pytest

from octoforge_core.datasets.api import (
    DatasetRecordValidationError,
    DatasetSchemaError,
    FieldType,
)
from octoforge_core.datasets.validation import dump_schema, parse_schema, validate_record

VALID_PAYLOAD: dict[str, Any] = {
    "item": "apple",
    "kcal": 95,
    "weight": 70.5,
    "eaten": True,
    "day": "2026-01-15",
    "moment": "2026-01-15T10:30:00",
}
FULL_SCHEMA_RAW = {
    "fields": [
        {"name": "item", "type": "string", "required": True},
        {"name": "kcal", "type": "integer"},
        {"name": "weight", "type": "number"},
        {"name": "eaten", "type": "boolean"},
        {"name": "day", "type": "date"},
        {"name": "moment", "type": "datetime"},
    ]
}
TWO_VIOLATIONS = 2


def test_parse_schema_ok() -> None:
    schema = parse_schema({"fields": [{"name": "item", "type": "string", "required": True}]})

    assert len(schema.fields) == 1
    field = schema.fields[0]
    assert field.name == "item"
    assert field.type is FieldType.STRING
    assert field.required is True


def test_parse_schema_required_defaults_to_false() -> None:
    schema = parse_schema({"fields": [{"name": "item", "type": "string"}]})

    assert schema.fields[0].required is False


def test_parse_schema_all_types() -> None:
    schema = parse_schema(FULL_SCHEMA_RAW)

    assert [field.type for field in schema.fields] == [
        FieldType.STRING,
        FieldType.INTEGER,
        FieldType.NUMBER,
        FieldType.BOOLEAN,
        FieldType.DATE,
        FieldType.DATETIME,
    ]


def test_schema_dump_parse_round_trip() -> None:
    schema = parse_schema(FULL_SCHEMA_RAW)

    assert parse_schema(dump_schema(schema)) == schema


def test_dump_schema_emits_explicit_required() -> None:
    dumped = dump_schema(parse_schema({"fields": [{"name": "item", "type": "string"}]}))

    assert dumped == {"fields": [{"name": "item", "type": "string", "required": False}]}


@pytest.mark.parametrize(
    "raw",
    [
        "not-an-object",
        {},
        {"fields": "not-a-list"},
        {"fields": ["not-an-object"]},
        {"fields": [{"type": "string"}]},
        {"fields": [{"name": "", "type": "string"}]},
        {"fields": [{"name": "item", "type": "unknown"}]},
        {"fields": [{"name": "item", "type": "string", "required": "yes"}]},
        {
            "fields": [
                {"name": "item", "type": "string"},
                {"name": "item", "type": "integer"},
            ]
        },
    ],
)
def test_parse_schema_rejected(raw: object) -> None:
    with pytest.raises(DatasetSchemaError):
        parse_schema(raw)


def test_validate_record_all_types_pass() -> None:
    validate_record(parse_schema(FULL_SCHEMA_RAW), VALID_PAYLOAD)


def test_validate_record_missing_required_field() -> None:
    payload = {key: value for key, value in VALID_PAYLOAD.items() if key != "item"}

    with pytest.raises(DatasetRecordValidationError, match="item"):
        validate_record(parse_schema(FULL_SCHEMA_RAW), payload)


def test_validate_record_null_violates_required_field() -> None:
    payload = {**VALID_PAYLOAD, "item": None}

    with pytest.raises(DatasetRecordValidationError, match="item"):
        validate_record(parse_schema(FULL_SCHEMA_RAW), payload)


def test_validate_record_optional_field_may_be_absent_or_null() -> None:
    schema = parse_schema({"fields": [{"name": "kcal", "type": "integer"}]})

    validate_record(schema, {})
    validate_record(schema, {"kcal": None})


@pytest.mark.parametrize(
    ("field_type", "value"),
    [
        ("string", 42),
        ("integer", "95"),
        ("integer", True),  # bool is not an integer
        ("integer", 95.0),
        ("number", "95"),
        ("number", True),  # bool is not a number
        ("boolean", 1),
        ("boolean", "true"),
        ("date", "15.01.2026"),
        ("date", "2026-01-15T10:30:00"),
        ("datetime", "not-a-moment"),
        ("datetime", 42),
    ],
)
def test_validate_record_type_mismatch(field_type: str, value: object) -> None:
    schema = parse_schema({"fields": [{"name": "field", "type": field_type}]})

    with pytest.raises(DatasetRecordValidationError, match="field"):
        validate_record(schema, {"field": value})


def test_validate_record_number_accepts_int_and_float() -> None:
    schema = parse_schema({"fields": [{"name": "value", "type": "number"}]})

    validate_record(schema, {"value": 95})
    validate_record(schema, {"value": 95.5})


def test_validate_record_extra_fields_allowed() -> None:
    schema = parse_schema({"fields": [{"name": "item", "type": "string", "required": True}]})

    validate_record(schema, {"item": "apple", "brand": "antonovka", "extra": [1, 2]})


def test_validate_record_collects_all_violations() -> None:
    schema = parse_schema(
        {
            "fields": [
                {"name": "item", "type": "string", "required": True},
                {"name": "kcal", "type": "integer"},
            ]
        }
    )

    with pytest.raises(DatasetRecordValidationError) as exc_info:
        validate_record(schema, {"kcal": "95"})

    assert len(exc_info.value.violations) == TWO_VIOLATIONS
