"""Shared HTTP transports and language-model clients."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from octoforge_core import build_llm_client
from octoforge_core.config import EmbeddingBackend, HttpRerankerConfig, RerankerConfig
from octoforge_core.llm.embedding_cache import CachingEmbeddingClient
from octoforge_core.llm.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from octoforge_core.llm.http_reranker import HttpRerankerClient
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder
from octoforge_core.llm.reranker import CrossEncoderReranker, RerankerClient
from octoforge_core.ports import LLMClient
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.serper import SerperSearchProvider
from octoforge_server.config import Settings


@dataclass(frozen=True, slots=True)
class HttpClients:
    llm: httpx.AsyncClient
    embeddings: httpx.AsyncClient
    vision: httpx.AsyncClient
    speech: httpx.AsyncClient
    outbound: httpx.AsyncClient


@dataclass(frozen=True, slots=True)
class LanguageServices:
    llm: LLMClient
    embedder: EmbeddingClient
    reranker: RerankerClient | None
    search: SearchProvider | None


@asynccontextmanager
async def open_http_clients(settings: Settings) -> AsyncIterator[HttpClients]:
    async with (
        httpx.AsyncClient(base_url=settings.llm_base_url) as llm,
        httpx.AsyncClient(base_url=settings.resolved_embedding_base_url()) as embeddings,
        httpx.AsyncClient(base_url=settings.resolved_vision_base_url()) as vision,
        httpx.AsyncClient(base_url=settings.stt_base_url) as speech,
        httpx.AsyncClient() as outbound,
    ):
        yield HttpClients(llm, embeddings, vision, speech, outbound)


def build_language_services(settings: Settings, clients: HttpClients) -> LanguageServices:
    return LanguageServices(
        build_llm_client(clients.llm, settings.to_llm_config()),
        _build_embedder(settings, clients.embeddings),
        build_reranker(settings, clients.outbound),
        _build_search_provider(settings, clients.outbound),
    )


def _build_embedder(settings: Settings, client: httpx.AsyncClient) -> EmbeddingClient:
    backend: EmbeddingClient
    if settings.embedding_backend == EmbeddingBackend.LOCAL:
        backend = SentenceTransformerEmbedder(
            model_name=settings.embedding_model,
            batch_size=settings.embedding_batch_size,
        )
    else:
        backend = OpenAIEmbeddingClient(client, settings.to_embedding_config())
    return CachingEmbeddingClient(backend)


def build_reranker(settings: Settings, client: httpx.AsyncClient) -> RerankerClient | None:
    """Build the optional reranker: HTTP with a key, otherwise local."""
    if not settings.reranker_model:
        return None
    if settings.reranker_api_key:
        return HttpRerankerClient(
            client,
            HttpRerankerConfig(
                model=settings.reranker_model,
                api_key=settings.reranker_api_key,
                api_url=settings.reranker_api_url,
                timeout_seconds=settings.reranker_timeout_seconds,
            ),
        )
    return CrossEncoderReranker(RerankerConfig(model=settings.reranker_model))


def _build_search_provider(
    settings: Settings,
    client: httpx.AsyncClient,
) -> SearchProvider | None:
    if not settings.serper_token:
        return None
    return SerperSearchProvider(client, settings.serper_token)
