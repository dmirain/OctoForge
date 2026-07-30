"""An EmbeddingClient decorator that stops paying twice for the same text.

`recall` searches instructions and datasets with the same query string, and
each service embeds it independently — two identical computations on nearly
every user message. With the local sentence-transformers backend that is real
CPU in a worker thread; with the HTTP backend it is a second round trip.

Fixing it by threading a precomputed vector through both services would widen
two ports with a parameter that exists purely as an optimisation. A decorator
in the composition root keeps `EmbeddingClient` a single `embed` method and
lets the caches disappear by not being wired.

Deliberately tiny and unmanaged: an LRU with no TTL. The mapping from text to
vector is stable for the life of a process (the model cannot change under it),
so entries never go stale — they only fall out when the cache is full.
"""

import asyncio
from collections import OrderedDict

from octoforge_core.llm.embeddings import EmbeddingClient

# Sized for the working set of a few concurrent turns, not for a corpus:
# roughly 1 MB at 1024 dimensions, which is the point where caching more stops
# buying anything because nobody asks the same question that long ago.
DEFAULT_CACHE_ENTRIES = 128


class CachingEmbeddingClient:
    """EmbeddingClient that remembers the last few texts it embedded."""

    def __init__(
        self,
        inner: EmbeddingClient,
        max_entries: int = DEFAULT_CACHE_ENTRIES,
    ) -> None:
        self._inner = inner
        self._max_entries = max_entries
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        # One in-flight computation per text: without it, two concurrent recalls
        # of the same query still both call the backend, which is exactly the
        # case this exists for.
        self._in_flight: dict[str, asyncio.Future[tuple[float, ...]]] = {}

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one vector per input text, computing only what is not cached."""
        if not texts:
            return ()
        missing = tuple(
            text
            for text in dict.fromkeys(texts)
            if text not in self._cache and text not in self._in_flight
        )
        if missing:
            await self._compute(missing)
        return tuple([await self._resolve(text) for text in texts])

    async def _compute(self, texts: tuple[str, ...]) -> None:
        """Embed the texts nobody else is already embedding, and publish them."""
        loop = asyncio.get_running_loop()
        futures = {text: loop.create_future() for text in texts}
        self._in_flight.update(futures)
        try:
            vectors = await self._inner.embed(texts)
        except BaseException as error:
            for text, future in futures.items():
                self._in_flight.pop(text, None)
                if not future.done():
                    future.set_exception(error)
            raise
        for text, vector in zip(texts, vectors, strict=True):
            self._remember(text, vector)
            self._in_flight.pop(text, None)
            if not futures[text].done():
                futures[text].set_result(vector)

    async def _resolve(self, text: str) -> tuple[float, ...]:
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return cached
        pending = self._in_flight.get(text)
        if pending is not None:
            return await pending
        (vector,) = await self._inner.embed((text,))
        self._remember(text, vector)
        return vector

    def _remember(self, text: str, vector: tuple[float, ...]) -> None:
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
