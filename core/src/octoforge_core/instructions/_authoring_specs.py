"""Contracts exposed to the model by instruction authoring tools."""

from typing import Any

from octoforge_core.instructions.types import InstructionType

SAVE_NAME = "instruction_save"
SAVE_DESCRIPTION = (
    "Create or update one of your instructions: "
    "knowledge — a durable fact useful to everyone (saved as your private record; "
    "an admin can publish it later), "
    "skill — a how-to scenario for a task that is not solvable upfront "
    "(e.g. to get the weather, call the weather endpoint via external_call), "
    "endpoint — an API request contract executed by external_call (like an MCP tool). "
    "The record belongs to you: only you can see and delete it. "
    "Existing (type, title) records of yours are replaced with a bumped version. "
    "If one of your records was published, you stay its author: saving the same "
    "(type, title) updates the published record for everyone. "
    "Personal facts about the user are not instructions: save them with memory_store."
)
ENDPOINT_CONTRACT_HINT = (
    "An endpoint's content is a JSON contract with these fields and no others "
    "(unknown ones are refused): method, url_template, params_schema, headers, "
    "body_template, auth (plus free-form notes/description). "
    "Placeholders work in url_template, body_template and header values: "
    "{param} is declared in params_schema, {user.code} is a per-user value an "
    "operator set, {secret.code} is a per-user secret — you see the name, never "
    'the value. Declare a secret as auth {"secret": "<code>", "format": '
    '"Bearer {value}"} or write {secret.<code>} into a header; auth "basic"/'
    '"bearer" as a bare word attaches NOTHING. params_schema types: "string" — '
    'one path segment, slashes escaped; "path" — several segments, slashes '
    'kept (what a CalDAV/discovery href needs); "host" — a hostname, requires '
    '"hosts": ["*.example.com"] and is the only type allowed where the URL\'s '
    "host stands, which is how a contract follows a service across sibling "
    "hosts instead of hard-coding one user's shard. Check secret_list for the "
    "codes a user actually has."
)
SAVED_TEMPLATE = "instruction saved: [{kind}] {title} (version {version})"
SAVABLE_KINDS = tuple(kind for kind in InstructionType if kind is not InstructionType.MEMORY)
SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [kind.value for kind in SAVABLE_KINDS],
            "description": "Instruction kind",
        },
        "title": {"type": "string", "description": "Unique (per type) instruction title"},
        "content": {
            "type": "string",
            "description": f"Instruction body. {ENDPOINT_CONTRACT_HINT}",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tags for searchability",
        },
    },
    "required": ["type", "title", "content"],
}

DELETE_NAME = "instruction_delete"
DELETE_DESCRIPTION = (
    "Delete one of your instructions by its id (ids come from recall "
    "results). Only your own records can be deleted."
)
DELETED_MESSAGE = "instruction deleted"
NOT_FOUND_MESSAGE = "instruction not found (only your own records can be deleted)"
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "Instruction id from recall results"}},
    "required": ["id"],
}
