"""Basic skill executing external calls described by tool instructions."""

from typing import Any

from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "external_call"
SKILL_DESCRIPTION = (
    "Execute an external call described by a tool instruction from the store. "
    "Use instructions_search to discover available tools, then call them by name "
    "with the params declared in the tool record."
)
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Title of the tool instruction to execute"},
        "params": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Parameters declared by the tool's params_schema",
        },
    },
    "required": ["name"],
}


class ExternalCallSkill:
    """Thin adapter over the ExternalCallExecutor."""

    def __init__(self, executor: ExternalCallExecutor) -> None:
        self._executor = executor

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, run the call and format status + body."""
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SkillArgumentsError("name must be a non-empty string")
        params = _parse_params(arguments.get("params"))
        result = await self._executor.execute(name, params)
        return f"HTTP {result.status}\n{result.body}"


def _parse_params(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        return dict(raw)
    raise SkillArgumentsError("params must be an object of strings")
