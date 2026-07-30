"""What this installation can actually do, reported once at startup.

Every optional capability of OctoForge is a port with an empty-configuration
switch: no key, no feature. That is the right default for an embeddable core —
and a bad first experience, because a missing `OF_EMBEDDING_*` used to mean the
agent quietly lost `recall` (its skills, knowledge and dataset search) with
nothing in the log to say so.

So the composition root states the object graph it just built: one block listing
every capability as on or off, with the endpoint or model that backs it. Values
are never logged — only URLs, model names and counts.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from octoforge_core.config import EmbeddingBackend
from octoforge_core.db.search_extensions import PG_TEXTSEARCH, VECTOR

from octoforge_web.config import Settings

REPORT_HEADER = "capabilities of this installation:"
ON = "on"
OFF = "off"
# Off is normal for most capabilities, but these two make the installation
# either useless (no recall) or unreachable (503 on every HTTP request), so
# they are worth a warning rather than a line in a list.
CRITICAL = frozenset({"embeddings", "operator credential"})


@dataclass(frozen=True, slots=True)
class Capability:
    """One reported capability: its state and what backs it."""

    name: str
    enabled: bool
    detail: str

    def line(self) -> str:
        """Render as a single log line."""
        return f"  {self.name:<20} {ON if self.enabled else OFF:<3}  {self.detail}"


def describe_capabilities(
    settings: Settings,
    search_extensions: frozenset[str] = frozenset(),
) -> tuple[Capability, ...]:
    """Describe every optional capability, in report order.

    `search_extensions` is what the database was probed for, so it is passed in
    rather than derived from settings: whether pgvector and pg_textsearch exist
    is a fact about the server, not about configuration, and no OF_ variable
    can be trusted to describe it.
    """
    return (
        *_llm_capabilities(settings),
        *_multimodal_capabilities(settings),
        *_tool_capabilities(settings),
        *_surface_capabilities(settings),
        *_search_capabilities(search_extensions),
    )


def log_capabilities(
    settings: Settings,
    logger: logging.Logger,
    search_extensions: frozenset[str] = frozenset(),
) -> None:
    """Log the capability report; critical gaps also get their own warning."""
    capabilities = describe_capabilities(settings, search_extensions)
    logger.info("%s\n%s", REPORT_HEADER, "\n".join(cap.line() for cap in capabilities))
    for cap in capabilities:
        if not cap.enabled and cap.name in CRITICAL:
            logger.warning("%s is off: %s", cap.name, cap.detail)


def _llm_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            name="llm",
            enabled=bool(settings.llm_api_key),
            detail=(
                f"{settings.llm_model} at {_host(settings.llm_base_url)}"
                if settings.llm_api_key
                else "set OF_LLM_API_KEY — no answer can be generated without it"
            ),
        ),
        Capability(
            name="embeddings",
            enabled=settings.embeddings_configured(),
            detail=_embeddings_detail(settings),
        ),
        Capability(
            name="reranker",
            enabled=bool(settings.reranker_model) or bool(settings.reranker_api_key),
            detail=_reranker_detail(settings),
        ),
    )


def _multimodal_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            name="vision",
            enabled=settings.vision_configured(),
            detail=(
                f"{settings.vision_model} at {_host(settings.resolved_vision_base_url())}"
                if settings.vision_configured()
                else "OF_VISION_MODEL is empty — images arrive as text placeholders"
            ),
        ),
        Capability(
            name="image_look tool",
            enabled=settings.deep_vision_configured(),
            detail=(
                settings.vision_deep_model
                if settings.deep_vision_configured()
                else "OF_VISION_DEEP_MODEL is empty — no re-examination of a picture"
            ),
        ),
        Capability(
            name="voice messages",
            enabled=settings.speech_configured(),
            detail=(
                f"{settings.stt_model} at {_host(settings.stt_base_url)}"
                if settings.speech_configured()
                else "set OF_STT_BASE_URL and OF_STT_MODEL to transcribe recordings"
            ),
        ),
    )


def _tool_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            name="web search",
            enabled=bool(settings.serper_token),
            detail=(
                "serper.dev"
                if settings.serper_token
                else "OF_SERPER_TOKEN is empty — the web_search tool stays hidden"
            ),
        ),
        Capability(
            name="secret store",
            enabled=bool(settings.secrets_key),
            detail=(
                f"Fernet, one-time links at {settings.resolved_public_base_url()}"
                if settings.secrets_key
                else "OF_SECRETS_KEY is empty — endpoints declaring auth.secret fail"
            ),
        ),
    )


def _surface_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            name="database",
            enabled=True,
            detail=_database_detail(settings.database_url),
        ),
        Capability(
            name="operator credential",
            enabled=bool(settings.admin_password_hash),
            detail=(
                f"HTTP Basic as {settings.admin_username!r}"
                if settings.admin_password_hash
                else "OF_ADMIN_PASSWORD_HASH is empty — the HTTP surface answers 503"
            ),
        ),
        Capability(
            name="telegram",
            enabled=bool(settings.telegram_bot_token),
            detail=_telegram_detail(settings),
        ),
    )


def _embeddings_detail(settings: Settings) -> str:
    if settings.embedding_backend == EmbeddingBackend.LOCAL:
        return f"local sentence-transformers, {settings.embedding_model}"
    if settings.embeddings_inherit_llm():
        return (
            f"{settings.embedding_model} at {_host(settings.resolved_embedding_base_url())}"
            " (inherited from OF_LLM_*)"
        )
    if settings.embeddings_configured():
        return f"{settings.embedding_model} at {_host(settings.embedding_base_url)}"
    return "no endpoint — recall, skills, knowledge and dataset search are unavailable"


def _reranker_detail(settings: Settings) -> str:
    if settings.reranker_api_key:
        return f"HTTP rerank at {_host(settings.reranker_api_url)}"
    if settings.reranker_model:
        return f"local cross-encoder, {settings.reranker_model}"
    return "OF_RERANKER_MODEL is empty — recall ranks by cosine only"


def _telegram_detail(settings: Settings) -> str:
    if not settings.telegram_bot_token:
        return "OF_TELEGRAM_BOT_TOKEN is empty — the adapter does not start"
    admins = len(settings.telegram_admin_ids)
    gate = f"invite gate, {admins} admin(s)" if admins else "OPEN TO EVERYONE (no admin ids)"
    return f"long-poll, {gate}"


def _database_detail(url: str) -> str:
    scheme = urlsplit(url).scheme
    dialect = scheme.split("+", 1)[0] if scheme else "unknown"
    single_writer = " (single writer: one process only)" if dialect == "sqlite" else ""
    return f"{dialect}{single_writer}"


def _host(url: str) -> str:
    """Host of a URL, or the raw value when it does not parse as one."""
    return urlsplit(url).netloc or url


def _search_capabilities(search_extensions: frozenset[str]) -> tuple[Capability, ...]:
    """Report what the database itself can contribute to retrieval.

    Neither is required. Without pgvector the whole visible table is pulled
    into the process and ranked there, which is correct but grows with the
    corpus; without pg_textsearch there is no lexical half at all. Both are
    unavailable on managed Postgres and on SQLite, so "off" here is a normal
    state worth stating plainly rather than a misconfiguration.
    """
    vector = VECTOR in search_extensions
    lexical = PG_TEXTSEARCH in search_extensions
    return (
        Capability(
            name="vector search",
            enabled=vector,
            detail=(
                "pgvector ranks in the database"
                if vector
                else "no pgvector — the visible table is ranked in process"
            ),
        ),
        Capability(
            name="lexical search",
            enabled=lexical,
            detail=(
                "pg_textsearch, BM25 over the russian_unaccent config"
                if lexical
                else "no pg_textsearch — recall is embeddings only"
            ),
        ),
    )
