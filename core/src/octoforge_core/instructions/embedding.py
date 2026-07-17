"""Embedding client port of the instructions module.

The concrete OpenAI-compatible implementation lives in
`octoforge_core.llm.embeddings`; the module receives it via constructor (DI).
"""

from typing import Protocol


class EmbeddingClient(Protocol):
    """Turns texts into dense vectors; one vector per input text, in order."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one embedding vector per input text, preserving order."""
        ...
