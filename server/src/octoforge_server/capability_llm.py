"""LLM, embeddings, reranker, vision and speech capability descriptions."""

from urllib.parse import urlsplit

from octoforge_core.config import EmbeddingBackend

from octoforge_server.capability_model import Capability
from octoforge_server.config import Settings


def llm_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            "llm",
            bool(settings.llm_api_key),
            (
                f"{settings.llm_model} at {host(settings.llm_base_url)}"
                if settings.llm_api_key
                else "set OF_LLM_API_KEY - no answer can be generated without it"
            ),
        ),
        Capability("embeddings", settings.embeddings_configured(), _embeddings_detail(settings)),
        Capability(
            "reranker",
            bool(settings.reranker_model) or bool(settings.reranker_api_key),
            _reranker_detail(settings),
        ),
    )


def multimodal_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            "vision",
            settings.vision_configured(),
            (
                f"{settings.vision_model} at {host(settings.resolved_vision_base_url())}"
                if settings.vision_configured()
                else "OF_VISION_MODEL is empty - images arrive as text placeholders"
            ),
        ),
        Capability(
            "image_look tool",
            settings.deep_vision_configured(),
            settings.vision_deep_model
            or "OF_VISION_DEEP_MODEL is empty - no re-examination of a picture",
        ),
        Capability(
            "voice messages",
            settings.speech_configured(),
            (
                f"{settings.stt_model} at {host(settings.stt_base_url)}"
                if settings.speech_configured()
                else "set OF_STT_BASE_URL and OF_STT_MODEL to transcribe recordings"
            ),
        ),
    )


def host(url: str) -> str:
    return urlsplit(url).netloc or url


def _embeddings_detail(settings: Settings) -> str:
    if settings.embedding_backend == EmbeddingBackend.LOCAL:
        return f"local sentence-transformers, {settings.embedding_model}"
    if settings.embeddings_inherit_llm():
        return (
            f"{settings.embedding_model} at {host(settings.resolved_embedding_base_url())} "
            "(inherited from OF_LLM_*)"
        )
    if settings.embeddings_configured():
        return f"{settings.embedding_model} at {host(settings.embedding_base_url)}"
    return "no endpoint - recall, skills, knowledge and dataset search are unavailable"


def _reranker_detail(settings: Settings) -> str:
    if settings.reranker_api_key:
        return f"HTTP rerank at {host(settings.reranker_api_url)}"
    if settings.reranker_model:
        return f"local cross-encoder, {settings.reranker_model}"
    return "OF_RERANKER_MODEL is empty - recall ranks by cosine only"
