"""Registration of tools backed by knowledge and archive services."""

from octoforge_core.composition_types import ToolLimits, ToolServices, ToolStores
from octoforge_core.context.tools import HistorySearchLimits, HistorySearchTool
from octoforge_core.datasets.tools import DataForgetTool, DataPutTool, DataQueryTool
from octoforge_core.instructions.tools import (
    InstructionDeleteTool,
    InstructionSaveTool,
    InstructionSearchTool,
)
from octoforge_core.memory.tools import MemoryDeleteTool, MemoryStoreTool
from octoforge_core.net.tools import EndpointGetTool, ExternalCallTool
from octoforge_core.tariffs.api import LimitGate
from octoforge_core.tools.registry import ToolRegistry


def register_knowledge_tools(
    registry: ToolRegistry,
    services: ToolServices,
    limits: ToolLimits,
) -> None:
    """Register instruction, endpoint and dataset tools."""
    registry.register(
        InstructionSearchTool(
            service=services.instructions,
            default_k=limits.instructions_top_k,
            datasets=services.datasets,
        )
    )
    registry.register(InstructionSaveTool(service=services.instructions))
    registry.register(InstructionDeleteTool(service=services.instructions))
    registry.register(EndpointGetTool(service=services.instructions))
    registry.register(ExternalCallTool(executor=services.executor))
    registry.register(DataPutTool(service=services.datasets))
    registry.register(
        DataQueryTool(
            service=services.datasets,
            default_limit=limits.datasets_query_default_limit,
            max_limit=limits.datasets_query_max_limit,
        )
    )
    registry.register(DataForgetTool(service=services.datasets))


def register_memory_tools(
    registry: ToolRegistry,
    services: ToolServices,
    limits: LimitGate | None,
) -> None:
    """Register memory writes over the shared instruction service."""
    registry.register(MemoryStoreTool(service=services.instructions, limits=limits))
    registry.register(MemoryDeleteTool(service=services.instructions))


def register_history_tool(
    registry: ToolRegistry,
    stores: ToolStores,
    limits: ToolLimits,
) -> None:
    """Register archive search over messages and summaries."""
    registry.register(
        HistorySearchTool(
            archive=stores.archive,
            summaries=stores.summaries,
            limits=HistorySearchLimits(
                default=limits.history_search_default_limit,
                maximum=limits.history_search_max_limit,
            ),
        )
    )
