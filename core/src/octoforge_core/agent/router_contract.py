"""Model-facing tool contract for message routing."""

from octoforge_core.agent.router_types import RouteAction
from octoforge_core.dialogs.api import TITLE_MAX_LENGTH
from octoforge_core.tools.base import ToolSpec

ROUTE_TOOL_NAME = "route"
ROUTE_TOOL_SPEC = ToolSpec(
    name=ROUTE_TOOL_NAME,
    description="Say which exchange the user message belongs to.",
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [action.value for action in RouteAction],
                "description": (
                    "new = the message opens its own exchange; continue = it belongs to an "
                    "existing one; command = pure control, with nothing to answer."
                ),
            },
            "exchange_id": {
                "type": ["string", "null"],
                "description": "The exchange for continue; null otherwise.",
            },
            "cancel_exchange_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exchanges the user explicitly asked to stop.",
            },
            "title": {
                "type": ["string", "null"],
                "description": (
                    "For continue: a short noun phrase naming the updated subject, up to "
                    f"{TITLE_MAX_LENGTH} characters. Null when the current name still fits."
                ),
            },
        },
        "required": ["action", "exchange_id", "cancel_exchange_ids", "title"],
    },
)
