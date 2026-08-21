"""Agent, persistence, scheduler and retention settings."""

import socket

from octoforge_core.agent.loop import DEFAULT_TOOL_TIMEOUT_SECONDS
from octoforge_core.retention import RetentionPolicy
from pydantic import BaseModel, Field

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./octoforge.db"


class RuntimeSettings(BaseModel):
    agent_max_iterations: int = 10
    agent_tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    instructions_top_k: int = 5
    database_url: str = DEFAULT_DATABASE_URL
    datasets_query_default_limit: int = 50
    datasets_query_max_limit: int = 200
    history_search_default_limit: int = 20
    history_search_max_limit: int = 100
    context_hot_max_chars: int = 12000
    context_compact_target_chars: int = 6000
    model_context_tokens: int = 0
    context_buffer_tokens: int = 2000
    max_processes: int = 5
    node_id: str = Field(default_factory=socket.gethostname)
    router_timeout_seconds: float = 10.0
    cron_poll_interval_seconds: float = 1.0
    material_quiet_seconds: float = 30.0
    material_sweep_interval_seconds: float = 10.0
    cron_lease_ttl_seconds: float = 60.0
    cron_replay_limit: int = 5
    mcp_sync_interval_seconds: float = 3600.0
    collections_inline_max_chars: int = 2000
    collections_ttl_seconds: float = 3600.0
    collections_max_per_user: int = 20
    collections_max_mb_per_user: int = 50
    collections_query_max_limit: int = 500
    collections_sweep_interval_seconds: float = 300.0
    collections_collect_max_pages: int = 20
    response_memory_max_mb: int = 2
    response_memory_budget_mb: int = 200
    response_get_default_chars: int = 8000
    response_get_max_chars: int = 100_000
    http_request_max_chars: int = 8000
    http_body_max_chars: int = 8000
    collections_query_default_chars: int = 8000
    collections_query_max_chars: int = 32_000
    retention_messages_days: int = 0
    retention_exchanges_days: int = 0
    retention_tasks_days: int = 0
    retention_usage_days: int = 0

    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            messages_days=self.retention_messages_days or None,
            exchanges_days=self.retention_exchanges_days or None,
            tasks_days=self.retention_tasks_days or None,
            usage_days=self.retention_usage_days or None,
        )
