"""OpenAI-compatible vision client: images ride as data-URL content parts."""

import asyncio
import base64
from typing import Any

import httpx

from octoforge_core.config import VisionConfig
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import TransportError, raise_for_error_status
from octoforge_core.llm.retry import Sleeper, retry_transient
from octoforge_core.vision.api import ImageData

COMPLETIONS_PATH = "/chat/completions"
PARSE_ERROR_MESSAGE = "Unexpected vision response payload"
DATA_URL_TEMPLATE = "data:{media_type};base64,{payload}"


class OpenAIVisionClient:
    """VisionClient implementation for OpenAI-compatible chat endpoints.

    Deliberately not reused from `llm/openai.py`: that client streams a whole
    agent turn with tools and history, while this one is a single blocking
    question about images. Sharing the machinery would drag streaming, tool
    parsing and usage plumbing into a path that needs none of it.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        config: VisionConfig,
        *,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._config = config
        self._sleeper = sleeper

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        """Answer `prompt` about the images; retries only transient failures."""
        if not images:
            return ""
        return await retry_transient(lambda: self._request(images, prompt), sleeper=self._sleeper)

    async def _request(self, images: tuple[ImageData, ...], prompt: str) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": _data_url(image)}} for image in images
        )
        try:
            response = await self._http.post(
                COMPLETIONS_PATH,
                json={
                    "model": self._config.model,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": self._config.max_tokens,
                },
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        raise_for_error_status(response)
        return _parse_answer(response.json())


def _data_url(image: ImageData) -> str:
    return DATA_URL_TEMPLATE.format(
        media_type=image.media_type,
        payload=base64.b64encode(image.content).decode(),
    )


def _parse_answer(data: dict[str, Any]) -> str:
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
