"""Instruction save and owner-scoped deletion tool adapters."""

from typing import Any

from octoforge_core.instructions._authoring_specs import (
    DELETE_DESCRIPTION,
    DELETE_NAME,
    DELETE_SCHEMA,
    DELETED_MESSAGE,
    NOT_FOUND_MESSAGE,
    SAVE_DESCRIPTION,
    SAVE_NAME,
    SAVE_SCHEMA,
    SAVED_TEMPLATE,
)
from octoforge_core.instructions.ports import InstructionService
from octoforge_core.instructions.requests import InstructionDefinition
from octoforge_core.instructions.types import (
    InstructionNotFoundError,
    InstructionType,
)
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class InstructionSaveTool:
    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(SAVE_NAME, SAVE_DESCRIPTION, SAVE_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        kind = _parse_kind(arguments.get("type"))
        if kind is InstructionType.MEMORY:
            raise ToolArgumentsError("personal memories are saved with memory_store, not here")
        if kind is InstructionType.SKILL and not feature_enabled(
            context.enabled_features, FeatureCode.SKILL_CREATE
        ):
            return feature_refusal(FeatureCode.SKILL_CREATE)
        title = _required_text(arguments.get("title"), "title")
        content = _required_text(arguments.get("content"), "content")
        definition = InstructionDefinition(kind, title, content, _parse_tags(arguments.get("tags")))
        instruction = await self._service.save(context.user_id, definition)
        return SAVED_TEMPLATE.format(
            kind=instruction.type.value,
            title=instruction.title,
            version=instruction.version,
        )


class InstructionDeleteTool:
    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(DELETE_NAME, DELETE_DESCRIPTION, DELETE_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        instruction_id = _required_text(arguments.get("id"), "id")
        try:
            await self._service.delete(context.user_id, instruction_id)
        except InstructionNotFoundError:
            return NOT_FOUND_MESSAGE
        return DELETED_MESSAGE


def _required_text(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{name} must be a non-empty string")
    return raw


def _parse_kind(raw: object) -> InstructionType:
    try:
        return InstructionType(str(raw))
    except ValueError as exc:
        raise ToolArgumentsError(f"unsupported instruction type: {raw!r}") from exc


def _parse_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    raise ToolArgumentsError("tags must be an array of strings")
