"""OpenAI-compatible chat-completion HTTP client."""

from collections.abc import AsyncIterator

import httpx

from octoforge_core.config import LLMConfig
from octoforge_core.domain import ChatMessage
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import (
    TransportError,
    araise_for_error_status,
    raise_for_error_status,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.openai_wire import (
    PARSE_ERROR_MESSAGE,
    OpenAIRequest,
    build_payload,
    parse_reply,
)
from octoforge_core.llm.stream_accumulator import StreamAccumulator
from octoforge_core.llm.stream_timing import StreamTiming
from octoforge_core.llm.usage import Completion, parse_usage
from octoforge_core.tools.base import ToolSpec

CHAT_COMPLETIONS_PATH = "/chat/completions"
SSE_DATA_PREFIX = "data:"
SSE_DONE_MARKER = "[DONE]"


class OpenAICompatibleClient:
    """LLM client for OpenAI-compatible complete and streaming endpoints."""

    def __init__(self, http_client: httpx.AsyncClient, config: LLMConfig) -> None:
        self._http = http_client
        self._config = config

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        try:
            response = await self._http.post(
                CHAT_COMPLETIONS_PATH,
                json=build_payload(OpenAIRequest(self._config.model, messages, tools, False)),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        raise_for_error_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        return Completion(parse_reply(data), parse_usage(data.get("usage")))

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        accumulator = StreamAccumulator()
        timing = StreamTiming()
        try:
            async with self._http.stream(
                "POST",
                CHAT_COMPLETIONS_PATH,
                json=build_payload(OpenAIRequest(self._config.model, messages, tools, True)),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            ) as response:
                await araise_for_error_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith(SSE_DATA_PREFIX):
                        continue
                    data = line[len(SSE_DATA_PREFIX) :].strip()
                    if data == SSE_DONE_MARKER:
                        break
                    events = accumulator.feed(data)
                    timing.observe(events)
                    for event in events:
                        yield event
                for event in accumulator.finish():
                    yield event
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        timing.log_summary(accumulator.ignored_fields)
        yield StreamFinished(accumulator.build_message(), accumulator.usage)
