"""Tests for the local sentence-transformers embedder."""

import numpy as np
import pytest

from octoforge_core.llm import local_embeddings
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder

MODEL_NAME = "fake-bi-encoder"
BATCH_SIZE = 8
EXPECTED_CALLS_ONE = 1


class FakeSentenceTransformer:
    """Stand-in recording encode calls and returning deterministic vectors."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.encode_calls: list[list[str]] = []
        self.batch_sizes: list[int] = []
        self.normalize_flags: list[bool] = []

    def encode(
        self,
        texts: list[str],
        batch_size: int,
        normalize_embeddings: bool,
    ) -> np.ndarray:
        self.encode_calls.append(list(texts))
        self.batch_sizes.append(batch_size)
        self.normalize_flags.append(normalize_embeddings)
        return np.array([[float(index), 1.0, 0.0] for index, _ in enumerate(texts)])


@pytest.fixture()
def created_models(monkeypatch: pytest.MonkeyPatch) -> list[FakeSentenceTransformer]:
    """Patch the model class with a factory collecting created instances."""
    created: list[FakeSentenceTransformer] = []

    def factory(model_name: str) -> FakeSentenceTransformer:
        instance = FakeSentenceTransformer(model_name)
        created.append(instance)
        return instance

    monkeypatch.setattr(local_embeddings, "SentenceTransformer", factory)
    return created


async def test_embed_returns_one_vector_per_text(
    created_models: list[FakeSentenceTransformer],
) -> None:
    embedder = SentenceTransformerEmbedder(MODEL_NAME, batch_size=BATCH_SIZE)

    result = await embedder.embed(("alpha", "beta"))

    assert result == ((0.0, 1.0, 0.0), (1.0, 1.0, 0.0))
    assert len(created_models) == EXPECTED_CALLS_ONE
    model = created_models[0]
    assert model.model_name == MODEL_NAME
    assert model.encode_calls == [["alpha", "beta"]]
    assert model.batch_sizes == [BATCH_SIZE]
    assert model.normalize_flags == [True]


async def test_model_loads_lazily_and_only_once(
    created_models: list[FakeSentenceTransformer],
) -> None:
    embedder = SentenceTransformerEmbedder(MODEL_NAME)

    assert created_models == []
    await embedder.embed(("first",))
    await embedder.embed(("second",))

    assert len(created_models) == EXPECTED_CALLS_ONE
    assert created_models[0].encode_calls == [["first"], ["second"]]


async def test_empty_input_short_circuits_without_loading_the_model(
    created_models: list[FakeSentenceTransformer],
) -> None:
    embedder = SentenceTransformerEmbedder(MODEL_NAME)

    assert await embedder.embed(()) == ()
    assert created_models == []
