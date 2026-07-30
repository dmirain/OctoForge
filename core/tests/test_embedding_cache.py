"""The embedding cache: pay once for a text, even under concurrency.

`recall` searches instructions and datasets with the same query, and each
service embeds it independently. With the local backend that is CPU in a worker
thread on nearly every user message; with the HTTP backend it is a second round
trip. These tests pin the behaviour the decorator exists for, and the failure
modes a naive cache would introduce.
"""

import asyncio

import pytest

from octoforge_core.llm.embedding_cache import CachingEmbeddingClient

VECTOR = (1.0, 0.0)
THREE_INPUTS = 3
TWO_ATTEMPTS = 2
# a, b, c, then a again after eviction
CALLS_WITH_EVICTION = 4
CACHE_SIZE = 2


class CountingEmbedder:
    """Records every batch it is asked for, so double work is visible."""

    def __init__(self, delay: float = 0.0) -> None:
        self.batches: list[tuple[str, ...]] = []
        self._delay = delay

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.batches.append(texts)
        if self._delay:
            await asyncio.sleep(self._delay)
        return tuple(VECTOR for _ in texts)


class FailingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        raise RuntimeError("backend down")


async def test_the_same_text_is_embedded_once() -> None:
    """The whole point: recall's two searches share one computation."""
    backend = CountingEmbedder()
    client = CachingEmbeddingClient(backend)

    first = await client.embed(("одна и та же строка",))
    second = await client.embed(("одна и та же строка",))

    assert first == second == (VECTOR,)
    assert len(backend.batches) == 1


async def test_concurrent_requests_for_one_text_share_a_single_call() -> None:
    """A plain dict cache would still let two simultaneous recalls both compute:
    nothing is cached until the first one finishes."""
    backend = CountingEmbedder(delay=0.02)
    client = CachingEmbeddingClient(backend)

    results = await asyncio.gather(
        client.embed(("общая строка",)),
        client.embed(("общая строка",)),
        client.embed(("общая строка",)),
    )

    assert results == [(VECTOR,), (VECTOR,), (VECTOR,)]
    assert len(backend.batches) == 1


async def test_only_the_uncached_part_of_a_batch_is_computed() -> None:
    backend = CountingEmbedder()
    client = CachingEmbeddingClient(backend)
    await client.embed(("known",))

    await client.embed(("known", "fresh"))

    assert backend.batches == [("known",), ("fresh",)]


async def test_results_stay_aligned_with_the_input_order() -> None:
    """Returning cached and freshly computed vectors in the wrong order would
    silently attach the wrong embedding to a record."""
    backend = CountingEmbedder()
    client = CachingEmbeddingClient(backend)
    await client.embed(("second",))

    result = await client.embed(("first", "second", "first"))

    assert len(result) == THREE_INPUTS
    assert result[0] == result[2]


async def test_a_failure_is_not_cached_and_leaves_nothing_pending() -> None:
    """A backend that was down must be retried, not remembered as broken."""
    backend = FailingEmbedder()
    client = CachingEmbeddingClient(backend)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await client.embed(("текст",))

    assert backend.calls == TWO_ATTEMPTS


async def test_concurrent_callers_all_see_a_failure() -> None:
    """The in-flight future must reject its waiters rather than hang them."""
    backend = FailingEmbedder()
    client = CachingEmbeddingClient(backend)

    results = await asyncio.gather(
        client.embed(("текст",)), client.embed(("текст",)), return_exceptions=True
    )

    assert all(isinstance(result, RuntimeError) for result in results)


async def test_the_cache_is_bounded() -> None:
    backend = CountingEmbedder()
    client = CachingEmbeddingClient(backend, max_entries=CACHE_SIZE)

    for text in ("a", "b", "c"):
        await client.embed((text,))
    await client.embed(("a",))  # evicted, so recomputed

    assert len(backend.batches) == CALLS_WITH_EVICTION


async def test_an_empty_request_touches_nothing() -> None:
    backend = CountingEmbedder()
    client = CachingEmbeddingClient(backend)

    assert await client.embed(()) == ()
    assert backend.batches == []
