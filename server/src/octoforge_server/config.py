"""Environment-driven application settings grouped by domain policy."""

from octoforge_core.config import VisionConfig
from pydantic_settings import BaseSettings, SettingsConfigDict

from octoforge_server.settings_llm import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    LlmSettings,
)
from octoforge_server.settings_media import DEFAULT_VISION_DEEP_MODEL, MediaSettings
from octoforge_server.settings_runtime import RuntimeSettings
from octoforge_server.settings_surface import SurfaceSettings

ENV_PREFIX = "OF_"
ENV_FILE = ".env"
DEFAULT_CRON_LEASE_TTL_SECONDS = 60.0
DEFAULT_CRON_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_CRON_REPLAY_LIMIT = 5
DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT = 50
DEFAULT_DATASETS_QUERY_MAX_LIMIT = 200
DEFAULT_INSTRUCTIONS_TOP_K = 5
DEFAULT_MAX_PROCESSES = 5
DEFAULT_ROUTER_TIMEOUT_SECONDS = 10.0
DEFAULT_SELF_BASE_URL = "http://127.0.0.1:8000"

__all__ = [
    "DEFAULT_CRON_LEASE_TTL_SECONDS",
    "DEFAULT_CRON_POLL_INTERVAL_SECONDS",
    "DEFAULT_CRON_REPLAY_LIMIT",
    "DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT",
    "DEFAULT_DATASETS_QUERY_MAX_LIMIT",
    "DEFAULT_EMBEDDING_BASE_URL",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_INSTRUCTIONS_TOP_K",
    "DEFAULT_MAX_PROCESSES",
    "DEFAULT_ROUTER_TIMEOUT_SECONDS",
    "DEFAULT_SELF_BASE_URL",
    "DEFAULT_VISION_DEEP_MODEL",
    "Settings",
]


class Settings(
    BaseSettings,
    LlmSettings,
    MediaSettings,
    RuntimeSettings,
    SurfaceSettings,
):
    """Flat `OF_*` settings interface composed from domain-specific policies."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        extra="ignore",
    )

    def resolved_vision_base_url(self) -> str:
        return self.resolve_vision_base_url(self.llm_base_url)

    def to_vision_config(self) -> VisionConfig:
        return self.vision_config(self.llm_api_key)

    def to_deep_vision_config(self) -> VisionConfig:
        return self.deep_vision_config(self.llm_api_key)
