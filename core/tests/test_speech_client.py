"""Tests for the OpenAI-compatible transcription client."""

from http import HTTPStatus

import httpx
import pytest

from octoforge_core.config import SpeechConfig
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import ProviderInternalError
from octoforge_core.speech.api import AudioData, upload_name
from octoforge_core.speech.client import OpenAITranscriptionClient

BASE_URL = "https://stt.example.com/v1"
API_KEY = "secret-key"
MODEL = "whisper-large-v3-turbo"
TRANSCRIPT = "привет, посмотри меню"
AUDIO_BYTES = b"opus-bytes"


def make_client(
    handler: httpx.MockTransport, language: str = ""
) -> tuple[OpenAITranscriptionClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(base_url=BASE_URL, transport=handler)
    config = SpeechConfig(api_key=API_KEY, model=MODEL, language=language)
    return OpenAITranscriptionClient(http_client=http, config=config), http


async def test_transcribe_uploads_the_recording_and_returns_its_text() -> None:
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(HTTPStatus.OK, json={"text": f" {TRANSCRIPT} "})

    client, http = make_client(httpx.MockTransport(handle))
    async with http:
        result = await client.transcribe(AudioData(content=AUDIO_BYTES, file_name="voice.ogg"))

    assert result == TRANSCRIPT  # surrounding whitespace is the provider's, not ours
    assert seen["url"] == f"{BASE_URL}/audio/transcriptions"
    assert seen["auth"] == f"Bearer {API_KEY}"
    body = seen["body"]
    assert isinstance(body, bytes)
    assert AUDIO_BYTES in body  # a multipart upload, not a JSON payload
    assert b'filename="voice.ogg"' in body
    assert MODEL.encode() in body
    assert b'name="language"' not in body  # unset: autodetection stays on


async def test_configured_language_is_sent_as_a_hint() -> None:
    seen: dict[str, bytes] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(HTTPStatus.OK, json={"text": TRANSCRIPT})

    client, http = make_client(httpx.MockTransport(handle), language="ru")
    async with http:
        await client.transcribe(AudioData(content=AUDIO_BYTES))

    assert b'name="language"' in seen["body"]
    assert b"ru" in seen["body"]


async def test_telegram_extension_is_normalized_before_the_upload() -> None:
    """Telegram hands out voice notes as `.oga`, which these APIs reject.

    Measured against the provider: `voice.oga` comes back as "file must be one
    of the following types", `voice.ogg` (the same Ogg container) transcribes.
    Getting this wrong fails every single voice message, so it is checked here
    rather than discovered in production.
    """
    seen: dict[str, bytes] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(HTTPStatus.OK, json={"text": TRANSCRIPT})

    client, http = make_client(httpx.MockTransport(handle))
    async with http:
        await client.transcribe(AudioData(content=AUDIO_BYTES, file_name="file_42.oga"))

    assert b'filename="file_42.ogg"' in seen["body"]


def test_upload_name_keeps_accepted_extensions_and_replaces_unknown_ones() -> None:
    assert upload_name("note.mp4") == "note.mp4"
    assert upload_name("song.MP3") == "song.MP3"  # case is the provider's problem, not the name's
    assert upload_name("voice.oga") == "voice.ogg"
    assert upload_name("recording.3gp") == "audio.ogg"
    assert upload_name("nameless") == "audio.ogg"


async def test_empty_audio_never_reaches_the_provider() -> None:
    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("an empty recording must not be uploaded")

    client, http = make_client(httpx.MockTransport(handle))
    async with http:
        assert await client.transcribe(AudioData(content=b"")) == ""


async def test_a_payload_without_text_is_a_response_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, json={"unexpected": "shape"})

    client, http = make_client(httpx.MockTransport(handle))
    async with http:
        with pytest.raises(LLMResponseError):
            await client.transcribe(AudioData(content=AUDIO_BYTES))


async def test_provider_failures_surface_as_llm_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR, text="boom")

    client, http = make_client(httpx.MockTransport(handle))
    async with http:
        with pytest.raises(ProviderInternalError):
            await client.transcribe(AudioData(content=AUDIO_BYTES))
