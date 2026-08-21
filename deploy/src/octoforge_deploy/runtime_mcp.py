"""MCP mirror composition and refresh lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from octoforge_core.instructions.api import InstructionService
from octoforge_core.mcp.client import McpClientConfig, StreamableHttpMcpClient
from octoforge_core.mcp.executor import McpMirrorCallExecutor
from octoforge_core.mcp.executor_types import McpExecutorOptions
from octoforge_core.mcp.skills import McpSkillGenerator
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore
from octoforge_core.mcp.sync import McpSyncLoop, McpSyncOptions, McpSyncServices, McpToolSync
from octoforge_core.mcp.tools import McpAddServices, McpAddTool
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.ports import LLMClient
from octoforge_core.secrets.api import SecretStore
from octoforge_server.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_deploy.runtime_http import HttpClients
from octoforge_deploy.runtime_responses import ResponseRuntime


@dataclass(frozen=True, slots=True)
class McpRuntime:
    sync: McpToolSync
    call_delegate: McpMirrorCallExecutor
    add_tool: McpAddTool


@dataclass(frozen=True, slots=True)
class McpBuildRequest:
    sessions: async_sessionmaker[AsyncSession]
    guard: SsrfGuard
    instructions: InstructionService
    secrets: SecretStore | None
    llm: LLMClient
    settings: Settings
    responses: ResponseRuntime


def build_mcp(request: McpBuildRequest, clients: HttpClients) -> McpRuntime:
    store = SqlAlchemyMcpServerStore(request.sessions)
    client = StreamableHttpMcpClient(
        clients.outbound,
        request.guard,
        McpClientConfig(
            max_response_bytes=request.responses.responses.memory.config.max_response_chars,
        ),
    )
    sync = McpToolSync(
        McpSyncServices(store, client, request.instructions),
        McpSyncOptions(
            secrets=request.secrets,
            skills=McpSkillGenerator(request.llm, request.instructions),
        ),
    )
    return McpRuntime(
        sync,
        McpMirrorCallExecutor(
            store,
            client,
            McpExecutorOptions(
                secrets=request.secrets,
                spill=request.responses.responses.spill,
                truncate_chars=request.settings.http_body_max_chars,
            ),
        ),
        McpAddTool(McpAddServices(store, sync, request.guard, request.secrets)),
    )


def start_mcp_sync(
    runtime: McpRuntime,
    settings: Settings,
) -> asyncio.Task[None]:
    loop = McpSyncLoop(runtime.sync, interval_seconds=settings.mcp_sync_interval_seconds)
    return asyncio.create_task(loop.run_forever())
