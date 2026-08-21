"""Model-facing names, descriptions, schemas and response templates for dataset tools."""

from typing import Any

PUT_NAME = "data_put"
PUT_DESCRIPTION = (
    "Write a record into one of the user's datasets - structured list-like data the "
    "user tracks over time (food/weight/habit trackers and the like; e.g. today's "
    "intake 'k2000 p100 f60 c120' goes to a food-log dataset with numeric fields). "
    "If the dataset does not exist yet it is created on the fly - then "
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
        "description": {"type": "string", "description": "Dataset purpose (creation only)"},
        "schema": {
            "type": "object",
            "description": "Dataset schema {'fields': [...]} (creation only)",
        },
        "usage_notes": {
            "type": "string",
            "description": "How to write/read/aggregate the data (creation only)",
        },
        "retention": {"type": "string", "description": "Retention policy (creation only)"},
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
    "properties": {"dataset": {"type": "string", "description": "Dataset name"}},
    "required": ["dataset"],
}
