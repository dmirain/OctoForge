"""Tests for the local cross-encoder reranker."""

import asyncio
import time

import numpy as np
import pytest

from octoforge_core.config import RerankerConfig
from octoforge_core.llm import reranker as reranker_module
from octoforge_core.llm.reranker import DEVICE_CPU, DEVICE_MPS, CrossEncoderReranker

MODEL_NAME = "fake-cross-encoder"
MAX_LENGTH = 256
BATCH_SIZE = 5
EXPECTED_LOADS_ONE = 1
CONCURRENT_CALLS = 2
LOAD_DELAY_SECONDS = 0.05


class FakeCrossEncoder:
    """Stand-in scoring pairs by combined text length."""

    def __init__(self, model: str, max_length: int, device: str) -> None:
        self.model = model
        self.max_length = max_length
        self.device = device
        self.predict_calls: list[list[tuple[str, str]]] = []

    def predict(
        self,
        pairs: list[tuple[str, str]],
        batch_size: int,
        show_progress_bar: bool,
    ) -> np.ndarray:
        self.predict_calls.append(list(pairs))
        return np.array([float(len(query) + len(candidate)) for query, candidate in pairs])


@pytest.fixture()
def created_models(monkeypatch: pytest.MonkeyPatch) -> list[FakeCrossEncoder]:
    """Patch the cross-encoder class with a factory collecting instances."""
    created: list[FakeCrossEncoder] = []

    def factory(model: str, max_length: int, device: str) -> FakeCrossEncoder:
        instance = FakeCrossEncoder(model, max_length, device)
        created.append(instance)
        return instance

    monkeypatch.setattr(reranker_module, "CrossEncoder", factory)
    return created


async def test_score_returns_one_float_per_pair_in_order(
    created_models: list[FakeCrossEncoder],
) -> None:
    reranker = CrossEncoderReranker(
        RerankerConfig(model=MODEL_NAME, max_length=MAX_LENGTH, batch_size=BATCH_SIZE)
    )

    result = await reranker.score((("ab", "cde"), ("longer query", "c")))

    assert result == (5.0, 13.0)
    assert len(created_models) == EXPECTED_LOADS_ONE
    model = created_models[0]
    assert model.model == MODEL_NAME
    assert model.max_length == MAX_LENGTH
    assert model.device in (DEVICE_MPS, DEVICE_CPU)
    assert model.predict_calls == [[("ab", "cde"), ("longer query", "c")]]


async def test_model_loads_lazily_and_only_once(
    created_models: list[FakeCrossEncoder],
) -> None:
    reranker = CrossEncoderReranker(RerankerConfig(model=MODEL_NAME))

    assert created_models == []
    await reranker.score((("a", "b"),))
    await reranker.score((("c", "d"),))

    assert len(created_models) == EXPECTED_LOADS_ONE


async def test_empty_input_short_circuits_without_loading_the_model(
    created_models: list[FakeCrossEncoder],
) -> None:
    reranker = CrossEncoderReranker(RerankerConfig(model=MODEL_NAME))

    assert await reranker.score(()) == ()
    assert created_models == []


async def test_concurrent_first_calls_load_the_model_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two parallel first calls race on the lazy load; the lock must admit one."""
    created: list[FakeCrossEncoder] = []

    def slow_factory(model: str, max_length: int, device: str) -> FakeCrossEncoder:
        time.sleep(LOAD_DELAY_SECONDS)  # widen the race window
        instance = FakeCrossEncoder(model, max_length, device)
        created.append(instance)
        return instance

    monkeypatch.setattr(reranker_module, "CrossEncoder", slow_factory)
    reranker = CrossEncoderReranker(RerankerConfig(model=MODEL_NAME))

    await asyncio.gather(
        *(reranker.score(((f"q{index}", "doc"),)) for index in range(CONCURRENT_CALLS))
    )

    assert len(created) == EXPECTED_LOADS_ONE
