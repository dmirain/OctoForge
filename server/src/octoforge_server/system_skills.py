"""Application system-skill pack of the web installer: the weather example.

Synced into the instructions store at startup together with the core registry
(see `instructions/registry.py`): one endpoint record plus the two scenarios
that use it. Editing happens here, not through the agent — the records are
system-owned.
"""

import json

from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.registry import SystemSkill

WEB_SYSTEM_SKILLS: tuple[SystemSkill, ...] = (
    SystemSkill(
        kind=InstructionType.ENDPOINT,
        title="wttr_in_weather",
        content=json.dumps(
            {
                "method": "GET",
                "url_template": "https://wttr.in/{city}?format=j2",
                "params_schema": {"city": {"type": "string", "required": True}},
                "auth": "none",
            }
        ),
        tags=("http", "weather", "example"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="get_current_weather",
        content=(
            "Scenario: report the current weather in a city.\n"
            "1. Call external_call with name 'wttr_in_weather' and params "
            '{"city": "<city>"}.\n'
            "2. From the JSON answer take current_condition[0]: temp_C, FeelsLikeC, "
            "weatherDesc[0].value, humidity.\n"
            "3. Answer the user with a short summary in the user's language."
        ),
        tags=("weather", "scenario", "example"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="mcp_integration",
        content=(
            "Scenario: connect an external MCP server and use its tools.\n"
            "1. Register the server: mcp_add with its Streamable HTTP url. The same "
            "url added by anyone stays one shared server; its tools become endpoint "
            "records named mcp/<server>/<tool>.\n"
            '2. If the server needs a token, pass auth={"secret": "<code>"} to '
            "mcp_add and tell the user to add THEIR OWN token via /secrets — every "
            "user's token is their own, there are no shared credentials.\n"
            "3. Discover tools with recall(type=endpoint, query=...), read a "
            "contract with endpoint_get, execute with external_call — arguments "
            "may be structured (objects, arrays) exactly as the input_schema says.\n"
            "4. Mirrored tools refresh on a periodic sync; if a call answers that "
            "the mirror is stale, re-run mcp_add with the same url to force it."
        ),
        tags=("mcp", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="compare_weather_two_cities",
        content=(
            "Scenario: compare the weather in two cities.\n"
            "1. Call external_call with name 'wttr_in_weather' for the first city, "
            "then for the second one.\n"
            "2. Take temp_C and weatherDesc[0].value from each JSON answer.\n"
            "3. Report both cities and the temperature difference."
        ),
        tags=("weather", "scenario", "example"),
    ),
)
