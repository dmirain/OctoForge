"""LLM, loop, routing, compaction and tariff composition."""

import httpx

from octoforge_core.agent.loop import AgentLoop, AgentLoopConfig
from octoforge_core.agent.prompts import PromptProvider
from octoforge_core.agent.router import LLMRouter, MessageRouter
from octoforge_core.config import LLMConfig
from octoforge_core.context.api import ContextCompactor
from octoforge_core.context.compactor import CompactorConfig, CompactorServices, LlmContextCompactor
from octoforge_core.cron.api import CronStore
from octoforge_core.cron.reporter import CronOutcomeReporter
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.retry import RetryingLLMClient, RetryPolicy
from octoforge_core.ports import LLMClient
from octoforge_core.tariffs.api import TariffStore, UsageMeter
from octoforge_core.tariffs.service import LimitService
from octoforge_core.tools.registry import ToolRegistry


def build_llm_client(http_client: httpx.AsyncClient, config: LLMConfig) -> LLMClient:
    """Build the OpenAI-compatible client with retries."""
    return RetryingLLMClient(
        OpenAICompatibleClient(http_client=http_client, config=config),
        RetryPolicy(
            max_retries=config.max_retries,
            base_seconds=config.retry_base_seconds,
            max_seconds=config.retry_max_seconds,
        ),
    )


def build_agent_loop(
    llm: LLMClient,
    registry: ToolRegistry,
    config: AgentLoopConfig,
) -> AgentLoop:
    """Build the agent loop over the given client and registry."""
    return AgentLoop(llm_client=llm, registry=registry, config=config)


def build_compactor(services: CompactorServices, config: CompactorConfig) -> ContextCompactor:
    """Build the topics-plus-hot-tail context compactor."""
    return LlmContextCompactor(services, config)


def build_router(
    llm: LLMClient,
    prompts: PromptProvider,
    *,
    timeout_seconds: float,
) -> MessageRouter:
    """Build the LLM message router."""
    return LLMRouter(llm, timeout_seconds=timeout_seconds, prompts=prompts)


def build_limit_service(tariffs: TariffStore, meter: UsageMeter) -> LimitService:
    """Build the tariff gate and usage ledger facade."""
    return LimitService(tariffs, meter)


def build_cron_outcome_reporter(store: CronStore) -> CronOutcomeReporter:
    """Build the listener folding cron outcomes back into the store."""
    return CronOutcomeReporter(store)
