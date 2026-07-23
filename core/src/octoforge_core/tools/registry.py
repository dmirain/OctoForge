"""Registry of tools available to the agent."""

from collections.abc import Callable

from octoforge_core.tools.base import Tool, ToolContext, ToolSpec
from octoforge_core.tools.errors import DuplicateToolError, ToolNotFoundError


class ToolRegistry:
    """Holds the registered tools under unique names."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool; names must be unique across the registry."""
        name = tool.spec.name
        if name in self._tools:
            raise DuplicateToolError(name)
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        """Return the tool by name."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def specs(self, context: ToolContext | None = None) -> list[ToolSpec]:
        """Return LLM-facing specs of the tools visible in the given context.

        A tool may opt into context-dependent visibility by defining a
        `visible_to(context) -> bool` attribute (duck-typed opt-in: the Tool
        protocol is unchanged and most tools stay visible always). Without a
        context every tool is listed, exactly as before.
        """
        if context is None:
            return [tool.spec for tool in self._tools.values()]
        return [tool.spec for tool in self._tools.values() if _is_visible(tool, context)]


def _is_visible(tool: Tool, context: ToolContext) -> bool:
    visible_to: Callable[[ToolContext], bool] | None = getattr(tool, "visible_to", None)
    return visible_to is None or visible_to(context)
