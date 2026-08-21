"""The data_forget tool."""

from typing import Any

from octoforge_core.datasets.service_port import DatasetService
from octoforge_core.datasets.tool_contract import (
    DELETED_TEMPLATE,
    FORGET_DESCRIPTION,
    FORGET_NAME,
    FORGET_SCHEMA,
    NOT_FOUND_TEMPLATE,
)
from octoforge_core.datasets.types import DatasetNotFoundError
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class DataForgetTool:
    """Delete one named dataset and report how many records it contained."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=FORGET_NAME,
            description=FORGET_DESCRIPTION,
            parameters_schema=FORGET_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        name = arguments.get("dataset")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("dataset must be a non-empty string")
        try:
            records_count = await self._service.delete_dataset(context.user_id, name)
        except DatasetNotFoundError:
            return NOT_FOUND_TEMPLATE.format(name=name)
        return DELETED_TEMPLATE.format(name=name, count=records_count)
