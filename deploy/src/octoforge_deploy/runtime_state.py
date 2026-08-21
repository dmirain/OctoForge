"""Typed stages of the assembled deployment."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from octoforge_core import ConversationManager
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.config import Settings
from octoforge_server.surfaces import Surface
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import HttpClients, LanguageServices
from octoforge_deploy.runtime_knowledge import KnowledgeServices
from octoforge_deploy.runtime_mcp import McpRuntime
from octoforge_deploy.runtime_media import MediaServices
from octoforge_deploy.runtime_responses import ResponseRuntime


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    settings: Settings
    telegram: TelegramSettings
    database: DatabaseRuntime
    clients: HttpClients
    language: LanguageServices
    knowledge: KnowledgeServices
    responses: ResponseRuntime
    media: MediaServices
    mcp: McpRuntime
    registry: ToolRegistry
    manager: ConversationManager
    surfaces: tuple[Surface, ...]


@dataclass(frozen=True, slots=True)
class RunningRuntime:
    graph: RuntimeGraph
    background: tuple[asyncio.Task[None], ...]
