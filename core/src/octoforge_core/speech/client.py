"""OpenAI-compatible transcription client: audio rides as a multipart upload."""

import asyncio
from typing import Any

import httpx

from octoforge_core.config import SpeechConfig
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import TransportError, raise_for_error_status
from octoforge_core.llm.retry import ShortRetryPolicy, Sleeper, retry_transient
from octoforge_core.speech.api import AudioData, upload_name

TRANSCRIPTIONS_PATH = "/audio/transcriptions"
PARSE_ERROR_MESSAGE = "Unexpected transcription response payload"


class OpenAITranscriptionClient:
    """TranscriptionClient for OpenAI-compatible `/audio/transcriptions` endpoints.

    A multipart upload rather than a JSON body, which is why this cannot
    reuse `llm/openai.py` or the vision client: same vendor shape, different
    request kind. `language` is passed when configured — a hint measurably
    helps accuracy and latency, and an empty one leaves autodetection on.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        config: SpeechConfig,
        *,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._config = config
        self._sleeper = sleeper

    async def transcribe(self, audio: AudioData) -> str:
        """Transcribe the recording; retries only transient failures."""
        if not audio.content:
            return ""
        return await retry_transient(
            lambda: self._request(audio),
            ShortRetryPolicy(sleeper=self._sleeper),
        )

    async def _request(self, audio: AudioData) -> str:
        data: dict[str, str] = {"model": self._config.model}
        if self._config.language:
            data["language"] = self._config.language
        files = {"file": (upload_name(audio.file_name), audio.content, audio.media_type)}
        try:
            response = await self._http.post(
                TRANSCRIPTIONS_PATH,
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        raise_for_error_status(response)
        return _parse_transcript(response.json())


def _parse_transcript(data: dict[str, Any]) -> str:
    try:
        return str(data["text"]).strip()
    except (KeyError, TypeError) as exc:
        raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
