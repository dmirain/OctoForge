"""Complete agent tool-registry composition."""

from octoforge_core.composition_knowledge_tools import (
    register_history_tool,
    register_knowledge_tools,
    register_memory_tools,
)
from octoforge_core.composition_types import ToolDependencies, ToolLimits
from octoforge_core.cron.tools import CronPauseTool, CronResumeTool
from octoforge_core.dialogs.tools import AskUserTool
from octoforge_core.net.tools import HttpRequestTool, HttpRequestToolConfig
from octoforge_core.search.tools import WebSearchTool
from octoforge_core.tasks.tools import TaskCreateTool, TaskDeleteTool, TaskListTool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_core.vision.tools import ImageLookTool


def build_tool_registry(dependencies: ToolDependencies, limits: ToolLimits) -> ToolRegistry:
    """Build the complete registry from runtime dependencies and policy limits."""
    registry = ToolRegistry()
    _register_runtime_tools(registry, dependencies, limits)
    registry.register(ImageLookTool(meter=dependencies.limit_gate))
    _register_optional_tools(registry, dependencies)
    register_knowledge_tools(registry, dependencies.services, limits)
    register_memory_tools(registry, dependencies.services, dependencies.limit_gate)
    register_history_tool(registry, dependencies.stores, limits)
    return registry


def _register_runtime_tools(
    registry: ToolRegistry,
    dependencies: ToolDependencies,
    limits: ToolLimits,
) -> None:
    stores = dependencies.stores
    registry.register(
        HttpRequestTool(
            dependencies.outbound_http,
            dependencies.guard,
            HttpRequestToolConfig(
                allowed_origins=limits.http_request_allowed_origins,
                spill=dependencies.services.responses.spill
                if dependencies.services.responses is not None
                else None,
                max_chars=limits.http_request_max_chars,
            ),
        )
    )
    registry.register(AskUserTool())
    registry.register(TaskCreateTool(cron_store=stores.cron, limits=dependencies.limit_gate))
    registry.register(TaskListTool(store=stores.tasks, cron_store=stores.cron))
    registry.register(TaskDeleteTool(store=stores.tasks, cron_store=stores.cron))
    registry.register(CronPauseTool(store=stores.cron))
    registry.register(CronResumeTool(store=stores.cron))


def _register_optional_tools(registry: ToolRegistry, dependencies: ToolDependencies) -> None:
    services = dependencies.services
    if services.collections is not None:
        registry.register(services.collections.query_tool)
        registry.register(services.collections.get_tool)
    if services.responses is not None:
        registry.register(services.responses.get_tool)
        registry.register(services.responses.find_tool)
        registry.register(services.responses.window_tool)
    if services.search_provider is not None:
        registry.register(WebSearchTool(provider=services.search_provider))
    for tool in (services.mcp_add, services.secret_list, services.secret_link):
        if tool is not None:
            registry.register(tool)
