"""Tests for the LLM error taxonomy and the retrying client wrapper."""

from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
import pytest

from octoforge_core import (
    AuthError,
    ChatMessage,
    ClientError,
    Completion,
    ContextOverflowError,
    LLMConfig,
    LLMResponseError,
    MessageRole,
    ProviderInternalError,
    QuotaError,
    RateLimitError,
    RetryingLLMClient,
    TransportError,
)
from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import Finished, RetryScheduled
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.llm.errors import classify_http_error, parse_retry_after
from octoforge_core.llm.events import RetryScheduled as LlmRetryScheduled
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.retry import RETRY_AFTER_DELAY_CAP_SECONDS
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry

BASE_URL = "https://llm.example.com/v1"
CTX = ToolContext(user_id="user-test", channel="web", dialog_id="dlg-test")
RETRY_REASON = "rate_limit"
RETRY_AFTER_SECONDS = 2.5
PARSED_RETRY_AFTER = 1.5
HEADER_RETRY_AFTER = 7.0
FLOOR_RETRY_AFTER = 2.0
LARGE_RETRY_AFTER = 3600.0
JITTER_SECONDS = 0.005
MAX_DELAY_SECONDS = 0.05
RETRIED_CALLS = 2
EXHAUSTED_CALLS = 3
HTTP_DATE_FUTURE = "Wed, 21 Oct 2099 07:28:00 GMT"
HTTP_DATE_PAST = "Wed, 21 Oct 2015 07:28:00 GMT"


def error_body(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def test_classify_rate_limit_with_retry_after() -> None:
    error = classify_http_error(
        HTTPStatus.TOO_MANY_REQUESTS, error_body("rate_limit", "slow down"), RETRY_AFTER_SECONDS
    )
    assert isinstance(error, RateLimitError)
    assert error.retry_after == RETRY_AFTER_SECONDS
    assert error.transient


def test_classify_auth() -> None:
    for status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        error = classify_http_error(status, error_body("invalid_api_key", "nope"), None)
        assert isinstance(error, AuthError)
        assert not error.transient


def test_classify_quota_by_status_and_body() -> None:
    assert isinstance(classify_http_error(HTTPStatus.PAYMENT_REQUIRED, None, None), QuotaError)
    by_body = classify_http_error(
        HTTPStatus.TOO_MANY_REQUESTS, error_body("insufficient_quota", "no credit"), None
    )
    # Body markers win over the status: 429 + insufficient_quota is a fatal
    # quota failure, not retriable throttling.
    assert isinstance(by_body, QuotaError)
    assert not by_body.transient
    bad_request = classify_http_error(
        HTTPStatus.BAD_REQUEST, error_body("insufficient_quota", "no credit"), None
    )
    assert isinstance(bad_request, QuotaError)


def test_classify_bare_429_without_body_markers() -> None:
    error = classify_http_error(HTTPStatus.TOO_MANY_REQUESTS, None, None)
    assert isinstance(error, RateLimitError)
    assert error.transient


def test_classify_context_overflow_by_body() -> None:
    error = classify_http_error(
        HTTPStatus.BAD_REQUEST,
        error_body("context_length_exceeded", "maximum context length reached"),
        None,
    )
    assert isinstance(error, ContextOverflowError)
    assert not error.transient


def test_classify_provider_internal_and_other_4xx() -> None:
    assert isinstance(
        classify_http_error(HTTPStatus.BAD_GATEWAY, None, None), ProviderInternalError
    )
    error = classify_http_error(HTTPStatus.NOT_FOUND, error_body("not_found", "no model"), None)
    assert isinstance(error, ClientError)
    assert not error.transient


def test_classify_transport_is_transient() -> None:
    assert TransportError("boom").transient


def test_classify_provider_internal_carries_retry_after() -> None:
    error = classify_http_error(HTTPStatus.SERVICE_UNAVAILABLE, None, RETRY_AFTER_SECONDS)
    assert isinstance(error, ProviderInternalError)
    assert error.retry_after == RETRY_AFTER_SECONDS
    assert error.transient


def test_parse_retry_after() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("1.5") == PARSED_RETRY_AFTER
    assert parse_retry_after("-3") is None
    assert parse_retry_after("tomorrow") is None


def test_parse_retry_after_http_date() -> None:
    future = parse_retry_after(HTTP_DATE_FUTURE)
    assert future is not None
    assert future > 0
    assert parse_retry_after(HTTP_DATE_PAST) == 0.0


async def test_openai_complete_raises_typed_error_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.TOO_MANY_REQUESTS,
            json=error_body("rate_limit", "throttled"),
            headers={"Retry-After": "7"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=LLMConfig(api_key="k", model="m"))
        with pytest.raises(RateLimitError) as caught:
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])
    assert caught.value.retry_after == HEADER_RETRY_AFTER


async def test_openai_stream_raises_typed_error_before_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=LLMConfig(api_key="k", model="m"))
        with pytest.raises(ProviderInternalError):
            async for _ in client.stream([ChatMessage(role=MessageRole.USER, content="hi")]):
                pass


class ScriptedFailureLLM:
    """LLMClient stub failing with scripted errors, then succeeding."""

    def __init__(self, failures: list[Exception], *, fail_mid_stream: bool = False) -> None:
        self._failures = list(failures)
        self._fail_mid_stream = fail_mid_stream
        self.calls = 0

    def _next_failure(self) -> Exception | None:
        self.calls += 1
        return self._failures.pop(0) if self._failures else None

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> Completion:
        failure = self._next_failure()
        if failure is not None:
            raise failure
        return Completion(message=ChatMessage(role=MessageRole.ASSISTANT, content="ok"))

    async def stream(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> AsyncIterator[StreamEvent]:
        failure = self._next_failure()
        if failure is not None:
            if self._fail_mid_stream:
                yield LlmTextDelta(text="partial")
            raise failure
        yield LlmTextDelta(text="ok")
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content="ok"))


class RecordingSleeper:
    """Sleeper stub capturing requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_retrying(inner: ScriptedFailureLLM, sleeper: RecordingSleeper) -> RetryingLLMClient:
    return RetryingLLMClient(
        inner,
        max_retries=2,
        base_seconds=0.01,
        max_seconds=MAX_DELAY_SECONDS,
        sleeper=sleeper,
    )


async def test_complete_retries_transient_failure() -> None:
    inner = ScriptedFailureLLM([RateLimitError("slow down")])
    sleeper = RecordingSleeper()
    client = make_retrying(inner, sleeper)

    reply = await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert reply.message.content == "ok"
    assert inner.calls == RETRIED_CALLS
    assert len(sleeper.delays) == 1
    assert 0 <= sleeper.delays[0] <= MAX_DELAY_SECONDS


async def test_complete_retry_delay_floored_at_retry_after() -> None:
    inner = ScriptedFailureLLM([RateLimitError("slow down", retry_after=FLOOR_RETRY_AFTER)])
    sleeper = RecordingSleeper()
    client = make_retrying(inner, sleeper)

    await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    # Retry-After is the floor; positive jitter is added on top.
    assert len(sleeper.delays) == 1
    assert FLOOR_RETRY_AFTER <= sleeper.delays[0] <= FLOOR_RETRY_AFTER + MAX_DELAY_SECONDS


async def test_retry_after_delay_includes_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("octoforge_core.llm.retry.random.uniform", lambda low, high: JITTER_SECONDS)
    inner = ScriptedFailureLLM([RateLimitError("slow down", retry_after=FLOOR_RETRY_AFTER)])
    sleeper = RecordingSleeper()
    client = make_retrying(inner, sleeper)

    await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert sleeper.delays == [FLOOR_RETRY_AFTER + JITTER_SECONDS]


async def test_retry_after_delay_is_capped() -> None:
    inner = ScriptedFailureLLM([RateLimitError("slow down", retry_after=LARGE_RETRY_AFTER)])
    sleeper = RecordingSleeper()
    client = make_retrying(inner, sleeper)

    await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert sleeper.delays == [RETRY_AFTER_DELAY_CAP_SECONDS]


async def test_retry_delay_floored_for_provider_internal_retry_after() -> None:
    inner = ScriptedFailureLLM([ProviderInternalError("boom", retry_after=FLOOR_RETRY_AFTER)])
    sleeper = RecordingSleeper()
    client = make_retrying(inner, sleeper)

    await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert len(sleeper.delays) == 1
    assert FLOOR_RETRY_AFTER <= sleeper.delays[0] <= FLOOR_RETRY_AFTER + MAX_DELAY_SECONDS


async def test_complete_does_not_retry_fatal_errors() -> None:
    for failure in (AuthError("bad key"), QuotaError("broke"), ContextOverflowError("too big")):
        inner = ScriptedFailureLLM([failure])
        client = make_retrying(inner, RecordingSleeper())
        with pytest.raises(type(failure)):
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])
        assert inner.calls == 1


async def test_complete_raises_after_exhausting_retries() -> None:
    inner = ScriptedFailureLLM([TransportError("a"), TransportError("b"), TransportError("c")])
    client = make_retrying(inner, RecordingSleeper())
    with pytest.raises(TransportError):
        await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])
    assert inner.calls == EXHAUSTED_CALLS


async def test_stream_retries_before_first_event_and_announces() -> None:
    inner = ScriptedFailureLLM([RateLimitError("slow down")])
    client = make_retrying(inner, RecordingSleeper())

    events = [
        event async for event in client.stream([ChatMessage(role=MessageRole.USER, content="hi")])
    ]

    assert isinstance(events[0], LlmRetryScheduled)
    assert events[0].attempt == 1
    assert events[0].reason == RETRY_REASON
    assert isinstance(events[-1], StreamFinished)
    assert inner.calls == RETRIED_CALLS


async def test_stream_does_not_retry_after_first_event() -> None:
    inner = ScriptedFailureLLM([TransportError("lost connection")], fail_mid_stream=True)
    client = make_retrying(inner, RecordingSleeper())

    with pytest.raises(TransportError):
        async for _ in client.stream([ChatMessage(role=MessageRole.USER, content="hi")]):
            pass
    assert inner.calls == 1


async def test_stream_does_not_retry_payload_errors() -> None:
    inner = ScriptedFailureLLM([LLMResponseError("garbage")])
    client = make_retrying(inner, RecordingSleeper())

    with pytest.raises(LLMResponseError):
        async for _ in client.stream([ChatMessage(role=MessageRole.USER, content="hi")]):
            pass
    assert inner.calls == 1


async def test_loop_maps_retry_scheduled_into_loop_events() -> None:
    inner = ScriptedFailureLLM([RateLimitError("slow down")])
    client = make_retrying(inner, RecordingSleeper())
    loop = AgentLoop(llm_client=client, registry=ToolRegistry(), max_iterations=3)

    events = [
        event
        async for event in loop.stream(
            [ChatMessage(role=MessageRole.USER, content="hi")], LoopControl(), CTX
        )
    ]

    retry_events = [event for event in events if isinstance(event, RetryScheduled)]
    assert len(retry_events) == 1
    assert retry_events[0].reason == RETRY_REASON
    assert isinstance(events[-1], Finished)


async def test_loop_fails_when_retries_exhausted() -> None:
    # The loop itself does not catch client errors: the exhausted error
    # propagates to the runner, which broadcasts `Failed` (see runner).
    failures = [TransportError("x") for _ in range(EXHAUSTED_CALLS)]
    inner = ScriptedFailureLLM(failures)
    client = make_retrying(inner, RecordingSleeper())
    loop = AgentLoop(llm_client=client, registry=ToolRegistry(), max_iterations=3)

    with pytest.raises(TransportError):
        async for _ in loop.stream(
            [ChatMessage(role=MessageRole.USER, content="hi")], LoopControl(), CTX
        ):
            pass
    assert inner.calls == EXHAUSTED_CALLS
