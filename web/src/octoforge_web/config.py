"""Application settings."""

from pathlib import Path
from typing import Annotated

from octoforge_core import EmbeddingConfig, LLMConfig
from octoforge_core.agent.loop import DEFAULT_TOOL_TIMEOUT_SECONDS
from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, SYSTEM_PROMPT_NAME
from octoforge_core.config import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    VISION_DEEP_MAX_TOKENS,
    EmbeddingBackend,
    SpeechConfig,
    VisionConfig,
)
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES
from octoforge_core.net.external import ExternalCallAuth
from octoforge_core.retention import RetentionPolicy
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from octoforge_web.skill_overlay import parse_overlay_source

ENV_PREFIX = "OF_"
ENV_FILE = ".env"
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_INSTRUCTIONS_TOP_K = 5
DEFAULT_AGENT_MAX_ITERATIONS = 10
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./octoforge.db"
DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT = 50
DEFAULT_DATASETS_QUERY_MAX_LIMIT = 200
DEFAULT_HISTORY_SEARCH_DEFAULT_LIMIT = 20
# 10 minutes of audio: a cap on both latency and the provider's daily quota
DEFAULT_VOICE_MAX_SECONDS = 600.0
DEFAULT_HISTORY_SEARCH_MAX_LIMIT = 100
DEFAULT_CONTEXT_HOT_MAX_CHARS = 12000
DEFAULT_CONTEXT_COMPACT_TARGET_CHARS = 6000
DEFAULT_MODEL_CONTEXT_TOKENS = 0
DEFAULT_CONTEXT_BUFFER_TOKENS = 2000
DEFAULT_MAX_PROCESSES = 5
DEFAULT_ROUTER_TIMEOUT_SECONDS = 10.0
DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS = 120.0
DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BASE_SECONDS = 1.0
DEFAULT_LLM_RETRY_MAX_SECONDS = 30.0
DEFAULT_SELF_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CRON_POLL_INTERVAL_SECONDS = 1.0
# how long forwarded material may stay quiet before the agent reacts on
# its own, and how often the sweep looks for settled collections
DEFAULT_MATERIAL_QUIET_SECONDS = 30.0
DEFAULT_MATERIAL_SWEEP_INTERVAL_SECONDS = 10.0
DEFAULT_CRON_LEASE_TTL_SECONDS = 60.0
DEFAULT_CRON_REPLAY_LIMIT = 5
DEFAULT_RERANKER_MODEL = ""
DEFAULT_RERANKER_API_URL = "https://api.siliconflow.cn/v1/rerank"
DEFAULT_RERANKER_TIMEOUT_SECONDS = 30.0
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 30.0
DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS = 1.5
DEFAULT_TELEGRAM_DATABASE_URL = "sqlite+aiosqlite:///./telegram.db"
DEFAULT_TELEGRAM_INVITE_TTL_SECONDS = 259200.0  # 3 days
FILE_SCHEME_PREFIX = "file:"
DEFAULT_ADMIN_USERNAME = "admin"
# the cheap tier of the two-tier vision setup (see core.vision.api); the
# stronger tier is picked elsewhere, only for explicit follow-up questions
DEFAULT_VISION_MODEL = "minimax-m3"
# the strong tier: spent only when `image_look` is called with a specific
# question. An empty value turns the tool off entirely while tier-1
# ingestion keeps working.
DEFAULT_VISION_DEEP_MODEL = "qwen3.5:397b"


class ExternalCallAuthSettings(BaseModel):
    """One whitelist entry for internal authorization on external calls."""

    base_url_prefix: str
    header_name: str
    header_value: str


class Settings(BaseSettings):
    """Environment-driven application settings."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_file=ENV_FILE, extra="ignore")

    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    # Left untouched, the embeddings endpoint inherits the LLM's URL and key
    # (see `embeddings_inherit_llm`): one OpenAI-compatible gateway serving
    # both is the common case, and the alternative was a silent loss of
    # `recall` — the agent's single most important tool.
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    embedding_api_key: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_backend: EmbeddingBackend = EmbeddingBackend.OPENAI
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    reranker_model: str = DEFAULT_RERANKER_MODEL
    reranker_candidates: int = DEFAULT_RERANK_CANDIDATES
    reranker_api_key: str = ""
    reranker_api_url: str = DEFAULT_RERANKER_API_URL
    reranker_timeout_seconds: float = DEFAULT_RERANKER_TIMEOUT_SECONDS
    instructions_top_k: int = DEFAULT_INSTRUCTIONS_TOP_K
    external_call_auth_whitelist: list[ExternalCallAuthSettings] = Field(default_factory=list)
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS
    database_url: str = DEFAULT_DATABASE_URL
    datasets_query_default_limit: int = DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT
    datasets_query_max_limit: int = DEFAULT_DATASETS_QUERY_MAX_LIMIT
    history_search_default_limit: int = DEFAULT_HISTORY_SEARCH_DEFAULT_LIMIT
    history_search_max_limit: int = DEFAULT_HISTORY_SEARCH_MAX_LIMIT
    context_hot_max_chars: int = DEFAULT_CONTEXT_HOT_MAX_CHARS
    context_compact_target_chars: int = DEFAULT_CONTEXT_COMPACT_TARGET_CHARS
    model_context_tokens: int = DEFAULT_MODEL_CONTEXT_TOKENS
    context_buffer_tokens: int = DEFAULT_CONTEXT_BUFFER_TOKENS
    max_processes: int = DEFAULT_MAX_PROCESSES
    router_timeout_seconds: float = DEFAULT_ROUTER_TIMEOUT_SECONDS
    llm_stream_idle_timeout_seconds: float = DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS
    # Wall-clock ceiling on one tool call. A tool that never returns holds its
    # iteration open forever and freezes that dialog until the process restarts.
    agent_tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    # How many days of each kind of row to keep. 0 means forever, which is the
    # default for every one of them: a retention policy switched on by an
    # upgrade would destroy data the installation believed it had.
    retention_messages_days: int = 0
    retention_exchanges_days: int = 0
    retention_tasks_days: int = 0
    llm_max_retries: int = DEFAULT_LLM_MAX_RETRIES
    llm_retry_base_seconds: float = DEFAULT_LLM_RETRY_BASE_SECONDS
    llm_retry_max_seconds: float = DEFAULT_LLM_RETRY_MAX_SECONDS
    self_base_url: str = DEFAULT_SELF_BASE_URL
    cron_poll_interval_seconds: float = DEFAULT_CRON_POLL_INTERVAL_SECONDS
    material_quiet_seconds: float = DEFAULT_MATERIAL_QUIET_SECONDS
    material_sweep_interval_seconds: float = DEFAULT_MATERIAL_SWEEP_INTERVAL_SECONDS
    cron_lease_ttl_seconds: float = DEFAULT_CRON_LEASE_TTL_SECONDS
    cron_replay_limit: int = DEFAULT_CRON_REPLAY_LIMIT
    telegram_bot_token: str = ""
    telegram_poll_timeout_seconds: float = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    telegram_edit_throttle_seconds: float = DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS
    telegram_database_url: str = DEFAULT_TELEGRAM_DATABASE_URL
    telegram_invite_ttl_seconds: float = DEFAULT_TELEGRAM_INVITE_TTL_SECONDS
    telegram_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # The bot's public @handle. Only the Bot API knows it, and nothing in the
    # code may assume it: with it configured, an invite is handed out as a
    # t.me deep link instead of a bare code the recipient has to paste.
    telegram_bot_username: str = ""
    # Origins the raw `http_request` tool may call, comma-separated. Empty is
    # the open web: the SSRF guard still blocks private space, but nothing stops
    # an agent that read a prompt-injected page from posting a dialog's contents
    # to a public address. An installation whose agents only ever call known
    # services closes that channel by listing them.
    http_request_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    serper_token: str = ""
    system_prompt_source: str = ""
    router_prompt_source: str = ""
    # Installer's lever on the system-skill registry: a `file:` JSON overlay
    # applied before the startup sync (replace/extend/add records without a
    # rebuild). See `skill_overlay.py`.
    system_skills_source: str = ""
    # Operator credential for the console and the HTTP API. The hash format is
    # `pbkdf2_sha256:iterations:salt:digest`; generate one with
    # `tools/hash_password.py`. Empty means the HTTP surface answers 503 rather
    # than serving an unauthenticated console (fail closed).
    admin_username: str = DEFAULT_ADMIN_USERNAME
    admin_password_hash: str = ""
    # Master key of the per-user secret store (Fernet, urlsafe base64; generate
    # with `python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"`). Empty disables the feature:
    # endpoints declaring auth.secret then answer with a clear error.
    secrets_key: str = ""
    # Public base URL of this installation (the /secrets one-time links are
    # built against it); defaults to the loopback self_base_url for dev.
    public_base_url: str = ""
    # The separate model that looks at images (the main LLM is text-only).
    # Base URL/key default to the main LLM's when unset — the common case is
    # one OpenAI-compatible gateway serving both. An empty model turns the
    # whole feature off (Telegram keeps today's placeholder/text-only path).
    vision_model: str = DEFAULT_VISION_MODEL
    vision_base_url: str = ""
    vision_api_key: str = ""
    # The strong vision tier (the `image_look` tool's re-examination of a
    # picture); shares the cheap tier's base URL/key fallbacks, only the
    # model differs. Empty means the tool is off.
    vision_deep_model: str = DEFAULT_VISION_DEEP_MODEL
    # Speech-to-text for voice messages. Unlike vision there is NO fallback to
    # the main LLM's endpoint: transcription is a different endpoint kind and a
    # chat-only gateway answers 404 for it, so both the URL and the model must
    # be set explicitly — either missing means the feature is off (a recording
    # keeps the "text only" notice).
    stt_base_url: str = ""
    stt_api_key: str = ""
    stt_model: str = ""
    stt_language: str = ""
    # Longest recording accepted: a cap on latency and on the provider's daily
    # audio quota, checked against the duration the update carries.
    voice_max_seconds: float = DEFAULT_VOICE_MAX_SECONDS

    def resolved_telegram_bot_username(self) -> str:
        """Bot handle without decoration: `@name`, a t.me URL or a bare name all work."""
        raw = self.telegram_bot_username.strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix) :]
                break
        return raw.strip("/").strip()

    def resolved_public_base_url(self) -> str:
        """Base URL for user-facing links: configured public one or self."""
        return (self.public_base_url or self.self_base_url).rstrip("/")

    def resolved_vision_base_url(self) -> str:
        """Base URL of the vision endpoint: configured one, else the main LLM's."""
        return self.vision_base_url or self.llm_base_url

    def to_vision_config(self) -> VisionConfig:
        """Build the core vision configuration; key defaults to the main LLM's."""
        return VisionConfig(
            api_key=self.vision_api_key or self.llm_api_key,
            model=self.vision_model,
        )

    def vision_configured(self) -> bool:
        """Whether the vision feature is on (a model is set); empty model = off."""
        return bool(self.vision_model)

    def to_deep_vision_config(self) -> VisionConfig:
        """Build the strong-tier vision configuration; mirrors `to_vision_config()`.

        Only the answer budget differs: this tier is asked about text the
        cheap description could not fit, and may be shown several pages.
        """
        return VisionConfig(
            api_key=self.vision_api_key or self.llm_api_key,
            model=self.vision_deep_model,
            max_tokens=VISION_DEEP_MAX_TOKENS,
        )

    def deep_vision_configured(self) -> bool:
        """Whether the strong vision tier (`image_look`) is on; empty model = off."""
        return bool(self.vision_deep_model)

    def to_speech_config(self) -> SpeechConfig:
        """Build the transcription configuration (own endpoint, no LLM fallback)."""
        return SpeechConfig(
            api_key=self.stt_api_key,
            model=self.stt_model,
            language=self.stt_language,
        )

    def speech_configured(self) -> bool:
        """Whether voice messages are transcribed; a missing URL or model = off."""
        return bool(self.stt_model and self.stt_base_url)

    def to_llm_config(self) -> LLMConfig:
        """Build the core LLM configuration."""
        return LLMConfig(
            api_key=self.llm_api_key,
            model=self.llm_model,
            max_retries=self.llm_max_retries,
            retry_base_seconds=self.llm_retry_base_seconds,
            retry_max_seconds=self.llm_retry_max_seconds,
        )

    def embeddings_inherit_llm(self) -> bool:
        """Whether the embeddings endpoint falls back to the main LLM's URL and key.

        True only when nothing embedding-specific was configured at all: the
        HTTP backend, no key of its own and the base URL still at its default.
        The condition is deliberately narrow so the fallback is monotone — it
        can only switch on a feature that used to be silently off, never
        redirect a deployment that named its own embeddings endpoint.
        """
        return (
            self.embedding_backend == EmbeddingBackend.OPENAI
            and not self.embedding_api_key
            and self.embedding_base_url == DEFAULT_EMBEDDING_BASE_URL
            and bool(self.llm_api_key)
        )

    def resolved_embedding_base_url(self) -> str:
        """Base URL of the embeddings endpoint: configured one, else the main LLM's."""
        return self.llm_base_url if self.embeddings_inherit_llm() else self.embedding_base_url

    def to_embedding_config(self) -> EmbeddingConfig:
        """Build the core embeddings configuration; key may come from the LLM's."""
        return EmbeddingConfig(
            api_key=self.llm_api_key if self.embeddings_inherit_llm() else self.embedding_api_key,
            model=self.embedding_model,
            backend=self.embedding_backend,
            batch_size=self.embedding_batch_size,
        )

    def embeddings_configured(self) -> bool:
        """Whether an embeddings backend is usable (drives seeding at startup).

        The LOCAL backend needs no credentials; the HTTP backend is usable with
        a key of its own or with the main LLM's, which it inherits when no
        embeddings endpoint was configured.
        """
        return (
            self.embedding_backend == EmbeddingBackend.LOCAL
            or bool(self.embedding_api_key)
            or self.embeddings_inherit_llm()
        )

    @field_validator("http_request_allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, value: object) -> object:
        """Accept a comma-separated string of origins (`.env` friendly)."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Accept a comma-separated string for the admin id list (`.env` friendly)."""
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    def to_external_call_auth_whitelist(self) -> tuple[ExternalCallAuth, ...]:
        """Build the executor's auth whitelist from the settings entries."""
        return tuple(
            ExternalCallAuth(
                base_url_prefix=entry.base_url_prefix,
                header_name=entry.header_name,
                header_value=entry.header_value,
            )
            for entry in self.external_call_auth_whitelist
        )

    def to_skills_overlay_path(self) -> Path | None:
        """Resolve `OF_SYSTEM_SKILLS_SOURCE` into a path; None when unset.

        Raises ValueError on an unsupported scheme: a mistyped source must
        fail at startup rather than silently serve the built-in registry.
        """
        if not self.system_skills_source:
            return None
        return parse_overlay_source(self.system_skills_source)

    def to_prompt_files(self) -> dict[str, Path]:
        """Map prompt names to their override files (`file:` scheme, empty = default).

        Raises ValueError on an unsupported scheme: a mistyped source must
        fail at startup, not silently serve the built-in prompt.
        """
        files: dict[str, Path] = {}
        sources = (
            (SYSTEM_PROMPT_NAME, self.system_prompt_source),
            (ROUTER_PROMPT_NAME, self.router_prompt_source),
        )
        for name, source in sources:
            if source:
                files[name] = _parse_prompt_source(source)
        return files

    def retention_policy(self) -> RetentionPolicy:
        """Build the retention policy; 0 reads as "keep forever", not "delete now"."""
        return RetentionPolicy(
            messages_days=self.retention_messages_days or None,
            exchanges_days=self.retention_exchanges_days or None,
            tasks_days=self.retention_tasks_days or None,
        )


def _parse_prompt_source(source: str) -> Path:
    if not source.startswith(FILE_SCHEME_PREFIX):
        raise ValueError(
            f"unsupported prompt source {source!r}: only '{FILE_SCHEME_PREFIX}' is supported"
        )
    return Path(source.removeprefix(FILE_SCHEME_PREFIX))
