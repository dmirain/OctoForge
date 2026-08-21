"""Contract exposed to the model by the recall tool."""

from typing import Any

from octoforge_core.instructions.types import InstructionType

SEARCH_NAME = "recall"
SEARCH_DESCRIPTION = (
    "Search everything you know: skill scenarios (how tasks are done here), "
    "knowledge (shared facts), this user's private memories and their dataset "
    "descriptors — one ranked search over all of it, with no type taking more "
    "than half of the hits. "
    "Pass 'type' to search one record kind only: type=memory for the user's "
    "memories alone, type=endpoint to discover external API endpoints (they are "
    "kept out of the default results — skills name the endpoints they use, and "
    "endpoint_get resolves a named endpoint's contract). "
    "Returns the top-k records with id, type, title, tags and full content: "
    "instructions first, dataset descriptors after them. "
    "This is the FIRST tool to call for any request that is not small talk, before "
    "you plan an approach or touch any other tool: the store usually already holds "
    "the scenario or the facts, and following a stored record beats designing your "
    "own way. It is a cheap local lookup — the cost of a redundant search is far "
    "below the cost of improvising past an existing one. Query with the intent "
    "plus the entity it concerns ('remind reminder', 'report user-data')."
)
MAX_K = 20
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for, in free text"},
        "k": {
            "type": "integer",
            "description": f"How many hits to return (1..{MAX_K})",
        },
        "type": {
            "type": "string",
            "enum": [kind.value for kind in InstructionType],
            "description": (
                "Optional record kind filter: knowledge, skill, memory or endpoint "
                "(endpoints appear only with this filter)"
            ),
        },
    },
    "required": ["query"],
}
