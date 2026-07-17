"""Basic skill saving instructions (knowledge/skills/tools) into the store."""

from typing import Any

from octoforge_core.instructions.api import InstructionService, InstructionType
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "instruction_save"
SKILL_DESCRIPTION = (
    "Create or update an instruction in the store: a durable fact (knowledge), "
    "an action scenario (skill) or a tool description (tool). "
    "Existing (type, title) records are replaced with a bumped version."
)
SAVED_TEMPLATE = "instruction saved: [{kind}] {title} (version {version})"
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [kind.value for kind in InstructionType],
            "description": "Instruction kind",
        },
        "title": {"type": "string", "description": "Unique (per type) instruction title"},
        "content": {"type": "string", "description": "Instruction body"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tags for searchability",
        },
    },
    "required": ["type", "title", "content"],
}


class InstructionSaveSkill:
    """Thin adapter over the InstructionService facade."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, upsert the instruction and confirm with the version."""
        kind = _parse_kind(arguments.get("type"))
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SkillArgumentsError("title must be a non-empty string")
        content = arguments.get("content")
        if not isinstance(content, str) or not content:
            raise SkillArgumentsError("content must be a non-empty string")
        tags = _parse_tags(arguments.get("tags"))
        instruction = await self._service.save(kind, title, content, tags)
        return SAVED_TEMPLATE.format(
            kind=instruction.type.value,
            title=instruction.title,
            version=instruction.version,
        )


def _parse_kind(raw: object) -> InstructionType:
    try:
        return InstructionType(str(raw))
    except ValueError as exc:
        raise SkillArgumentsError(f"unsupported instruction type: {raw!r}") from exc


def _parse_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    raise SkillArgumentsError("tags must be an array of strings")
