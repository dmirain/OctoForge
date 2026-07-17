"""Embeddings: the EmbeddingClient port and its OpenAI-compatible implementation.

The port lives next to the concrete client (`llm/`) because several modules
(instructions, datasets) depend on it; each receives it via constructor (DI).
"""

from typing import Any, Protocol

import httpx

from octoforge_core.config import EmbeddingConfig
from octoforge_core.errors import LLMResponseError

EMBEDDINGS_PATH = "/embeddings"
PARSE_ERROR_MESSAGE = "Unexpected embeddings response payload"


class EmbeddingClient(Protocol):
    """Turns texts into dense vectors; one vector per input text, in order."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one embedding vector per input text, preserving order."""
        ...


class OpenAIEmbeddingClient:
    """EmbeddingClient implementation for OpenAI-compatible endpoints."""

    def __init__(self, http_client: httpx.AsyncClient, config: EmbeddingConfig) -> None:
        self._http = http_client
        self._config = config

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one embedding vector per input text, preserving input order."""
        response = await self._http.post(
            EMBEDDINGS_PATH,
            json={"model": self._config.model, "input": list(texts)},
            headers={"Authorization": f"Bearer {self._config.api_key}"},
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        return self._parse_response(response.json(), expected=len(texts))

    @staticmethod
    def _parse_response(
        data: dict[str, Any],
        *,
        expected: int,
    ) -> tuple[tuple[float, ...], ...]:
        """Extract embeddings from the wire payload, ordered by their index."""
        try:
            items = data["data"]
        except (KeyError, TypeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        if not isinstance(items, list) or len(items) != expected:
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        vectors: list[tuple[float, ...]] = []
        try:
            for position, item in enumerate(sorted(items, key=lambda raw: raw["index"])):
                if item["index"] != position:
                    raise LLMResponseError(PARSE_ERROR_MESSAGE)
                vectors.append(tuple(float(component) for component in item["embedding"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        return tuple(vectors)
