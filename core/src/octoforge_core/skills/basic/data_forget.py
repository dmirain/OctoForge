"""Basic skill deleting a whole user-owned dataset with all its records."""

from typing import Any

from octoforge_core.datasets.api import DatasetNotFoundError, DatasetService
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "data_forget"
SKILL_DESCRIPTION = (
    "Delete one of the user's datasets with all its records. "
    "Use it when the user asks to forget everything about a tracked topic."
)
DELETED_TEMPLATE = "dataset '{name}' deleted with {count} record(s)"
NOT_FOUND_TEMPLATE = "dataset '{name}' not found"
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset": {"type": "string", "description": "Dataset name"},
    },
    "required": ["dataset"],
}


class DataForgetSkill:
    """Thin adapter over the DatasetService facade."""

    def __init__(self, service: DatasetService) -> None:
        self._service = service

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, delete the dataset and report the record count."""
        name = arguments.get("dataset")
        if not isinstance(name, str) or not name.strip():
            raise SkillArgumentsError("dataset must be a non-empty string")
        try:
            records_count = await self._service.delete_dataset(context.user_id, name)
        except DatasetNotFoundError:
            return NOT_FOUND_TEMPLATE.format(name=name)
        return DELETED_TEMPLATE.format(name=name, count=records_count)
