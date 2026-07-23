"""Tests for ToolRegistry."""

from typing import Any

import pytest

from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import DuplicateToolError, ToolNotFoundError
from octoforge_core.tools.registry import ToolRegistry

TOOL_NAME = "dummy"
OTHER_TOOL_NAME = "other"
UNKNOWN_TOOL_NAME = "missing"
CTX = ToolContext(user_id="user-test", channel="web", dialog_id="dlg-test")


class DummyTool:
    """Minimal tool stub."""

    def __init__(self, name: str) -> None:
        self._spec = ToolSpec(name=name, description="stub", parameters_schema={})

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return "ok"


def test_register_and_get() -> None:
    registry = ToolRegistry()
    tool = DummyTool(TOOL_NAME)

    registry.register(tool)

    assert registry.get(TOOL_NAME) is tool


def test_register_duplicate_name_rejected() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool(TOOL_NAME))

    with pytest.raises(DuplicateToolError):
        registry.register(DummyTool(TOOL_NAME))


def test_get_unknown_tool_raises() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError):
        registry.get(UNKNOWN_TOOL_NAME)


def test_specs_lists_all_registered() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool(TOOL_NAME))
    registry.register(DummyTool(OTHER_TOOL_NAME))

    names = [spec.name for spec in registry.specs()]

    assert names == [TOOL_NAME, OTHER_TOOL_NAME]


class GatedTool(DummyTool):
    """Tool with context-dependent visibility (duck-typed opt-in)."""

    def __init__(self, name: str, allowed_user_id: str) -> None:
        super().__init__(name)
        self._allowed_user_id = allowed_user_id

    def visible_to(self, context: ToolContext) -> bool:
        return context.user_id == self._allowed_user_id


def test_specs_without_context_lists_gated_skills() -> None:
    registry = ToolRegistry()
    registry.register(GatedTool(TOOL_NAME, allowed_user_id="someone-else"))

    names = [spec.name for spec in registry.specs()]

    assert names == [TOOL_NAME]


def test_specs_with_context_honors_visible_to() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool(OTHER_TOOL_NAME))
    registry.register(GatedTool(TOOL_NAME, allowed_user_id=CTX.user_id))

    visible = [spec.name for spec in registry.specs(CTX)]
    stranger = ToolContext(user_id="user-stranger", channel=CTX.channel, dialog_id="dlg-x")
    hidden = [spec.name for spec in registry.specs(stranger)]

    assert visible == [OTHER_TOOL_NAME, TOOL_NAME]
    assert hidden == [OTHER_TOOL_NAME]
