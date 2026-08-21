"""Endpoint contract lookup and execution tools."""

from dataclasses import replace
from typing import Any

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)
from octoforge_core.net.external import (
    CallOptions,
    ExternalCallContext,
    ExternalCallExecutor,
)
from octoforge_core.net.tool_contract import (
    CALL_DESCRIPTION,
    CALL_NAME,
    CALL_SCHEMA,
    ENDPOINT_GET_DESCRIPTION,
    ENDPOINT_GET_NAME,
    ENDPOINT_GET_SCHEMA,
    ENDPOINT_NOT_FOUND_TEMPLATE,
    ENDPOINT_TEMPLATE,
)
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class EndpointGetTool:
    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(ENDPOINT_GET_NAME, ENDPOINT_GET_DESCRIPTION, ENDPOINT_GET_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("name must be a non-empty string")
        try:
            record = await self._service.get_by_name(
                name,
                InstructionType.ENDPOINT,
                user_id=context.user_id,
            )
        except InstructionNotFoundError:
            return ENDPOINT_NOT_FOUND_TEMPLATE.format(name=name)
        return ENDPOINT_TEMPLATE.format(
            title=record.title,
            tags=", ".join(record.tags) if record.tags else "-",
            content=record.content,
        )


class ExternalCallTool:
    def __init__(self, executor: ExternalCallExecutor) -> None:
        self._executor = executor

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(CALL_NAME, CALL_DESCRIPTION, CALL_SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("name must be a non-empty string")
        result = await self._executor.execute(
            name,
            _parse_params(arguments.get("params")),
            ExternalCallContext(
                context.user_id,
                replace(_parse_call_options(arguments), scope=context.owner_task_id or ""),
            ),
        )
        return f"HTTP {result.status}\n{result.body}" if result.status else result.body


def _parse_params(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(isinstance(key, str) for key in raw):
        return dict(raw)
    raise ToolArgumentsError("params must be an object")


def _parse_call_options(arguments: dict[str, Any]) -> CallOptions:
    collect = arguments.get("collect", False)
    if not isinstance(collect, bool):
        raise ToolArgumentsError("collect must be a boolean")
    max_pages = arguments.get("max_pages")
    if max_pages is not None and (isinstance(max_pages, bool) or not isinstance(max_pages, int)):
        raise ToolArgumentsError("max_pages must be an integer")
    into = arguments.get("into")
    if into is not None and (not isinstance(into, str) or not into.strip()):
        raise ToolArgumentsError("into must be a collection ref like 'col:...' ")
    label = arguments.get("label", "")
    if not isinstance(label, str):
        raise ToolArgumentsError("label must be a string")
    return CallOptions(
        collect,
        max_pages,
        into.strip() if isinstance(into, str) else None,
        label,
    )
